// Shell session (M23-28): the one mutable session object that replaced the module-level

// bindings of entry.tsx, the shared service dependencies and the late-bound action table

// feature modules reach each other through. Bounded caches keep their original limits.

import type { ProfilerOnRenderCallback } from "react";

import type { PresentationMeasurement } from "../runtime/visualCompositionSession";
import type { AuthoringViewState } from "../state/authoringViewState";
import {
  type AppModeDraft,
  initialAppModeDraft,
} from "../components/H3Sidebar";
import type { ProductionViewState } from "../components/ProductionWorkbench";
import type { ProductionWorkbenchProjection } from "../contracts/productionWorkbenchCodec";
import type { ProductionAccumulatedProject } from "../contracts/productionAccumulationCodec";
import type { SidebarStagesDraft } from "../components/SidebarStages";
import type {
  AssistedFailureId,
  AssistedPromptProposalProjection,
} from "../contracts/assistedPromptProposalCodec";
import type { GenerationSequenceProjection } from "../contracts/generationSequenceCodec";
import type { ProductShellProjection } from "../contracts/projectionCodecs";
import type {
  ProviderIntent,
  ProviderSettingsProjection,
} from "../contracts/providerSettingsCodec";
import type { SemanticProposalReviewHandle } from "../contracts/semanticProposalReviewCodec";
import type { SidebarWorkspaceProjection } from "../contracts/sidebarWorkspaceCodec";
import type { TransactionTransparencyProjection } from "../contracts/transactionTransparencyCodec";
import {
  type AppModeAdmission,
  type AppModeInputs,
  type AppModeStartOptions,
  type AppModeStartResult,
  createAppModeController,
  type ManagedAppModePreparation,
} from "../host/appMode";
import { createAuthoringActionClient } from "../host/authoringActions";
import { createAuthoringMediaPreviewClient } from "../host/authoringMediaPreview";
import {
  type BuildProvenanceProjection,
  createBuildProvenanceClient,
} from "../host/buildProvenanceClient";
import { createDurationResolutionClient } from "../host/durationResolutionClient";
import type { createComfyPromptHistoryClient } from "../host/comfyPromptHistory";
import { createGenerationSequenceDriver } from "../host/generationSequence";
import { createInputGeometryClient } from "../host/inputGeometry";
import type {
  createManagedSequenceClient,
  createManagedSequenceReattachStore,
} from "../host/managedSequence";
import type {
  LanguageSettingsAdapter,
  LanguageSettingSnapshot,
} from "../host/languageSettings";
import type { OwnedGraphReference } from "../host/ownedGraphIdentity";
import type { createProductionAuthoringImportClient } from "../host/productionAuthoringImportClient";
import type { createProductionPlanningClient } from "../host/productionPlanningActions";
import type { createManagedQualificationClient } from "../host/managedQualificationActions";
import type { createAuthoringMediaSourceLeaseClient } from "../host/authoringMediaSourceLease";
import type { createAuthoringOutputCapabilityClient } from "../host/authoringOutputCapabilityClient";
import type { createMediaRuntimeClient } from "../host/mediaRuntimeClient";
import type { createWorkspaceStateClient } from "../host/workspaceStateClient";
import type { createRetainedAssetsClient } from "../host/retainedAssetsClient";
import type { createProjectDocumentClient } from "../host/projectDocumentClient";
import type { createProjectRecoveryClient } from "../host/projectRecoveryClient";
import type { createProjectPersistenceSession } from "./projectPersistenceSession";
import type { createOutputClient } from "../host/authoringOutputActions";
import type { createOutputPreview } from "../host/authoringOutputPreview";
import {
  initialMediaRuntimeState,
  type MediaRuntimeState,
} from "../state/mediaRuntimeState";
import {
  initialNleWorkspaceState,
  type NleWorkspaceState,
} from "../state/nleWorkspaceState";
import { createProductionActionClient } from "../host/productionActions";
import type { ProductionDestinationStore } from "../host/productionDestination";
import type { ManagedProjectDestinationAdmission } from "./managedProjectMember";
import { createProductionGenerationController } from "../host/productionGeneration";
import {
  createProductionMediaPreviewClient,
  type ProductionMediaPreviewState,
} from "../host/productionMediaPreview";
import {
  createProductionProposalDispatcher,
  type ProductionProposalSourceClaim,
} from "../host/productionProposalDispatcher";
import { createProviderSettingsClient } from "../host/providerSettingsActions";
import {
  createSequenceCoordinatorClient,
  type CoordinatorProductionMemberAuthority,
  type SequenceCoordinatorResult,
} from "../host/sequenceCoordinator";
import { createSidebarActionClient } from "../host/sidebarActions";
import {
  createSidebarHost,
  type ExecutionTerminalEvent,
  type SaveVideoArtifactEvent,
} from "../host/sidebarHost";
import { createLocaleStore } from "../i18n/localeStore";
import { createExtensionSetupLifecycle } from "./extensionSetup";
import { createAppModeInterpreter } from "./interpreter";
import { createMountController } from "./mountController";
import type { SidebarFocusKey } from "../navigation/focusRegistry";
import { createPageRegistry, type PageId } from "../navigation/pageRegistry";
import { createFrontendPerformanceRecorder } from "../performance/performanceBudget";
import { createDurationResolutionController } from "../state/durationResolutionState";
import { initialShellState, type ShellState } from "../state/shellState";
import { createSidebarRetention } from "../state/sidebarRetention";
import {
  initialWorkspaceState,
  type SidebarWorkspaceState,
} from "../state/sidebarWorkspace";
import type { HostApi, HostApp } from "../host/hostModuleTypes";
import type { createAppModeSession } from "./appModeSession";
import type { createAppModeCorrelation } from "./appModeCorrelation";
import type { createProductionSession } from "../host/productionSession";
import type { createAuthoringSession } from "../host/authoringSession";
import type { createProviderSession } from "../host/providerSession";
import type { createPresentationBinding } from "./presentationBinding";
import type { createNleWorkspaceSession } from "./nleWorkspaceSession";
import type { createMediaRuntimeSession } from "./mediaRuntimeSession";
import type { createWorkspaceStateLifecycle } from "./workspaceStateSession";
import type { createRetainedAssetsLifecycle } from "./retainedAssetsSession";
import type { createExtensionRegistration } from "./extensionRegistration";

export type ProductionEnsureAdmission = Readonly<{
  contextWorkspaceHandle: string;
  identity: string;
  key: string;
  appModeRun: number;
  viewGeneration: number;
}>;

export type ViewFocusClaim = Readonly<{
  container: HTMLElement;
  generation: number;
}>;

export type ManagedCanvasIdentity = Readonly<{
  workflowAuthority: object;
  ownedReference: OwnedGraphReference;
  ownedProjectionFingerprint: string;
}>;

export type ManagedBootstrapProjection = Readonly<{
  projection: ProductShellProjection;
  workspace: SidebarWorkspaceProjection;
  transactionTransparency?: TransactionTransparencyProjection;
  semanticProposalReview?: SemanticProposalReviewHandle;
}>;

export type ManagedBootstrapWaiter = {
  run: number;
  executionNodeId: string;
  promptId?: string;
  buffered: Map<string, ManagedBootstrapProjection>;
  terminal: Map<string, ExecutionTerminalEvent["kind"]>;
  resolve(value: ManagedBootstrapProjection): void;
  reject(error: Error): void;
  close(): void;
};

export type ManagedBootstrapHandle = Readonly<{
  bindPrompt(promptId: string): Promise<ManagedBootstrapProjection>;
  reject(error: Error): void;
}>;

export type ActiveManagedRun = {
  run: number;
  prepared: ManagedAppModePreparation;
  bootstrap: ManagedBootstrapProjection;
  authority: SequenceCoordinatorResult;
  acceptingModelPromptId?: string;
  modelPromptId?: string;
  pendingProjectionPromptIds: Set<string>;
  pendingTerminals: Map<string, ExecutionTerminalEvent["kind"]>;
  pendingArtifacts: Map<string, SaveVideoArtifactEvent>;
  artifactRecovery?: Readonly<{ event: SaveVideoArtifactEvent }>;
  artifactRetryInFlight: boolean;
  transition: Promise<void>;
  completed: boolean;
  projectMember?: Readonly<{
    admission: ManagedProjectDestinationAdmission;
    member: CoordinatorProductionMemberAuthority;
    projection: ProductionAccumulatedProject | ProductionWorkbenchProjection;
    proposalSource?: ProductionProposalSourceClaim;
  }>;
};

export function createShellSession() {
  return {
    /**
     * Depth of canvas writes App Mode is performing right now.
     *
     * The host announces every graph configure to its extensions, and this
     * extension's `beforeConfigureGraph` hook treats that announcement as the user
     * switching workflows underneath a run: it abandons the run, drops the
     * workspace and Production state, and reports the transaction cancelled. That
     * is right for a foreign switch and wrong for this module's own materialization
     * (M17-20 D11), which reaches the canvas through the same host seam -- on a real
     * host every App Mode run cancelled itself the instant it wrote the graph.
     *
     * A depth counter rather than a boolean because the rollback path can write the
     * canvas again while unwinding. It is raised only around the write itself and
     * lowered in a `finally`, so a configure that is genuinely the user's still
     * cancels the run.
     */
    ownedGraphConfigureDepth: 0,
    // M17-20 D1: the backend decides whether this host can generate. The shell asks
    // once per task mode, caches the answer and re-renders when it lands; it never
    // derives one, and a pending answer blocks nothing here because the App Mode
    // start gate re-checks and fails closed before any queue submission.
    appModeAdmissions: new Map<string, AppModeAdmission>(),
    appModeAdmissionsPending: new Set<string>(),
    appModeAdmissionAttemptRevision: new Map<string, number>(),
    appModeDraftRevision: 0,
    state: initialShellState as ShellState,
    workspaceState: initialWorkspaceState as SidebarWorkspaceState,
    transactionTransparency: undefined as
      TransactionTransparencyProjection | undefined,
    buildProvenance: undefined as BuildProvenanceProjection | undefined,
    buildProvenanceRequested: false,
    container: undefined as HTMLElement | undefined,
    actionAbort: undefined as AbortController | undefined,
    assistedAbort: undefined as AbortController | undefined,
    assistedProposal: undefined as AssistedPromptProposalProjection | undefined,
    assistedBusy: false,
    assistedFailure: undefined as AssistedFailureId | undefined,
    productionState: { status: "absent" } as ProductionViewState,
    productionAccumulationNotice: undefined as
      | Readonly<{
          kind: "added";
          projectOrdinal: number;
          segmentOrdinal: number;
          viewUpdated: boolean;
        }>
      | undefined,
    retryableManagedProductionMember: undefined as
      | Readonly<{
          workflow: object | undefined;
          workspaceHandle: string;
          workspaceId: string;
          segmentId: string;
        }>
      | undefined,
    authoringState: { status: "absent" } as AuthoringViewState,
    /** M25-16: overlay, import, planning, readiness and sequence projection; mount memory only. */
    nleWorkspace: initialNleWorkspaceState as NleWorkspaceState,
    /** M25-33: Media tools status, the one setup job and at most one continuation; mount memory. */
    mediaRuntime: initialMediaRuntimeState as MediaRuntimeState,
    /**
     * M25-21: bounded editing-state retention across navigation and view release. Only
     * complete extension disposal clears it; view release tears down resources, not drafts.
     */
    retention: createSidebarRetention(),
    authoringRequestCounter: 0,
    productionAbort: undefined as AbortController | undefined,
    productionEnsureAttemptedIdentities: new Set<string>(),
    productionEnsureClosedIdentities: new Set<string>(),
    productionEnsureActive: undefined as ProductionEnsureAdmission | undefined,
    productionMediaPreviewAbort: undefined as AbortController | undefined,
    productionMediaPreview: { status: "closed" } as ProductionMediaPreviewState,
    acceptedGenerationSequence: undefined as
      GenerationSequenceProjection | undefined,
    productionSessionHandle: undefined as string | undefined,
    productionContextBinding: undefined as
      | Readonly<{
          productionWorkspaceHandle: string;
          productionWorkspaceId: string;
          contextWorkspaceHandle: string;
        }>
      | undefined,
    providerSessionHandle: undefined as string | undefined,
    productionRequestSequence: 0,
    retryableProductionRequest: undefined as
      Readonly<{ key: string; requestId: string }> | undefined,
    languageSettingsAdapter: undefined as LanguageSettingsAdapter | undefined,
    languageSettingsSnapshot: Object.freeze({
      status: "setting_storage_unavailable",
      value: undefined,
      pending: false,
    }) as LanguageSettingSnapshot,
    languageSettingsDispose: undefined as (() => void) | undefined,
    /**
     * M22-06 provider state.
     *
     * It stays `undefined` until the seam answers once, so the Settings surface
     * renders nothing rather than an empty control that reads as broken. Nothing
     * here holds a credential: the value is read from the field, sent, and dropped.
     */
    providerSettingsProjection: undefined as
      ProviderSettingsProjection | undefined,
    providerSettingsRejection: undefined as string | undefined,
    providerSettingsBusy: false,
    providerSettingsBusyIntent: undefined as ProviderIntent | undefined,
    providerSettingsAbort: undefined as AbortController | undefined,
    providerSettingsGeneration: 0,
    providerCredentialClearer: undefined as (() => void) | undefined,
    widthDispose: undefined as (() => void) | undefined,
    focusTrackingDispose: undefined as (() => void) | undefined,
    localeDispose: undefined as (() => void) | undefined,
    localeSubscriptionDispose: undefined as (() => void) | undefined,
    pageSubscriptionDispose: undefined as (() => void) | undefined,
    lastViewFocusKey: undefined as SidebarFocusKey | undefined,
    pageFocusKeys: new Map<PageId, SidebarFocusKey>(),
    pendingPageFocus: undefined as
      Readonly<{ page: PageId; key: SidebarFocusKey }> | undefined,
    pendingSidebarFocusKey: undefined as SidebarFocusKey | undefined,
    viewFocusGeneration: 0,
    viewFocusClaim: undefined as ViewFocusClaim | undefined,
    appModeDraft: initialAppModeDraft as AppModeDraft,
    workspaceDraft: undefined as SidebarStagesDraft | undefined,
    pendingAppMode: undefined as
      | {
          run: number;
          inputs: AppModeInputs;
          options: AppModeStartOptions;
          result?: AppModeStartResult;
          managedIdentity?: ManagedCanvasIdentity;
          productionAdmission?: ManagedProjectDestinationAdmission;
        }
      | undefined,
    lastAppModeRequest: undefined as
      { inputs: AppModeInputs; options?: AppModeStartOptions } | undefined,
    ignoredProjectionPromptIds: new Set<string>(),
    acceptedProjectionPromptIds: new Set<string>(),
    ignoreProjectionUntilGraphRefresh: false,
    cancelledProjectionSuppression: undefined as
      | Readonly<{
          kind: "graph";
          graphFingerprint: string;
          executionNodeId?: string;
        }>
      | Readonly<{ kind: "managed"; identity: ManagedCanvasIdentity }>
      | undefined,
    graphChangedDuringRun: false,
    nativePreferenceRecoveryRefresh: false,
    executedGraphRefresh: undefined as
      | { run: number; graphFingerprint: string; executionNodeId: string }
      | undefined,
    deferredAppModeProjections: new Map<
      string,
      { run: number; executionNodeId: string; accept: () => void }
    >(),
    activeAppModeExecution: undefined as
      { run: number; promptId: string } | undefined,
    deferredAppModeTerminals: new Map<
      string,
      { run: number; kind: ExecutionTerminalEvent["kind"] }
    >(),
    deferredManagedArtifacts: new Map<
      string,
      { run: number; event: SaveVideoArtifactEvent }
    >(),
    pendingManagedArtifactAdmission: undefined as
      Readonly<{ run: number }> | undefined,
    managedBootstrapWaiter: undefined as ManagedBootstrapWaiter | undefined,
    activeManagedRun: undefined as ActiveManagedRun | undefined,
    acceptedManagedIdentity: undefined as ManagedCanvasIdentity | undefined,
    managedRequestSequence: 0,
    completedManagedPromptIds: new Set<string>(),
    diagnosticRun: 0,
    diagnosticRunsByAppRun: new Map<number, number>(),
    lastDiagnosticState: "",
    priorManagedStartSnapshot: undefined as unknown,
    /** One reconnect drives one reconciliation; the host repeats `reconnected` on every retry. */
    hostReconcileInFlight: false,
  };
}

export type ShellSession = ReturnType<typeof createShellSession>;

export type ShellDeps = Readonly<{
  app: HostApp;
  api: HostApi;
  mount: ReturnType<typeof createMountController>;
  entrySetup: ReturnType<typeof createExtensionSetupLifecycle>;
  localeStore: ReturnType<typeof createLocaleStore>;
  pageRegistry: ReturnType<typeof createPageRegistry>;
  performanceRecorder: ReturnType<typeof createFrontendPerformanceRecorder>;
  host: ReturnType<typeof createSidebarHost>;
  actions: ReturnType<typeof createSidebarActionClient>;
  productionActions: ReturnType<typeof createProductionActionClient>;
  productionDestinations: ProductionDestinationStore;
  managedSequenceClient: ReturnType<typeof createManagedSequenceClient>;
  managedSequenceReattachStore: ReturnType<
    typeof createManagedSequenceReattachStore
  >;
  comfyPromptHistoryClient: ReturnType<typeof createComfyPromptHistoryClient>;
  sequenceCoordinator: ReturnType<typeof createSequenceCoordinatorClient>;
  authoringActions: ReturnType<typeof createAuthoringActionClient>;
  authoringMediaPreviewClient: ReturnType<
    typeof createAuthoringMediaPreviewClient
  >;
  providerSettingsActions: ReturnType<typeof createProviderSettingsClient>;
  productionMediaPreviewClient: ReturnType<
    typeof createProductionMediaPreviewClient
  >;
  durationResolutionClient: ReturnType<typeof createDurationResolutionClient>;
  inputGeometryClient: ReturnType<typeof createInputGeometryClient>;
  buildProvenanceClient: ReturnType<typeof createBuildProvenanceClient>;
  durationResolutionController: ReturnType<
    typeof createDurationResolutionController
  >;
  productionProposalDispatcher: ReturnType<
    typeof createProductionProposalDispatcher
  >;
  appModeController: ReturnType<typeof createAppModeController>;
  appModeLifecycle: ReturnType<typeof createAppModeInterpreter>;
  generationSequenceDriver: ReturnType<typeof createGenerationSequenceDriver>;
  productionGenerationController: ReturnType<
    typeof createProductionGenerationController
  >;
  productionAuthoringImportClient: ReturnType<
    typeof createProductionAuthoringImportClient
  >;
  productionPlanningClient: ReturnType<typeof createProductionPlanningClient>;
  managedQualificationClient: ReturnType<
    typeof createManagedQualificationClient
  >;
  authoringMediaSourceLeaseClient: ReturnType<
    typeof createAuthoringMediaSourceLeaseClient
  >;
  authoringOutputCapabilityClient: ReturnType<
    typeof createAuthoringOutputCapabilityClient
  >;
  authoringOutputClient: ReturnType<typeof createOutputClient>;
  mediaRuntimeClient: ReturnType<typeof createMediaRuntimeClient>;
  workspaceStateClient?: ReturnType<typeof createWorkspaceStateClient>;
  retainedAssetsClient?: ReturnType<typeof createRetainedAssetsClient>;
  projectDocumentClient?: ReturnType<typeof createProjectDocumentClient>;
  projectRecoveryClient?: ReturnType<typeof createProjectRecoveryClient>;
  authoringOutputPreview: ReturnType<typeof createOutputPreview>;
  /**
   * Optional measurement observers for the two sibling subtrees. Normal entry wiring omits
   * them, so profiling adds no wrapper unless explicitly supplied by the performance harness.
   */
  h3SidebarProfilerObserver?: ProfilerOnRenderCallback;
  nleOverlayProfilerObserver?: ProfilerOnRenderCallback;
  /**
   * M25-45 AC45-04: one sample per frame the monitor actually presented. Omitted by normal entry
   * wiring, and the composition session then arms no measurement animation frame at all.
   */
  nlePresentationObserver?: (sample: PresentationMeasurement) => void;
}>;

export type ShellActions = ReturnType<typeof createAppModeSession> &
  ReturnType<typeof createAppModeCorrelation> &
  ReturnType<typeof createProductionSession> &
  ReturnType<typeof createAuthoringSession> &
  ReturnType<typeof createProviderSession> &
  ReturnType<typeof createPresentationBinding> &
  ReturnType<typeof createNleWorkspaceSession> &
  ReturnType<typeof createMediaRuntimeSession> &
  ReturnType<typeof createWorkspaceStateLifecycle> &
  ReturnType<typeof createRetainedAssetsLifecycle> &
  ReturnType<typeof createProjectPersistenceSession>;

export type ShellRuntime = Readonly<{
  session: ShellSession;
  deps: ShellDeps;
  actions: ShellActions;
}>;
