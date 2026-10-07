import { api } from "../../scripts/api.js";
import { app } from "../../scripts/app.js";
import { createAppModeController } from "./host/appMode";
import { createAuthoringActionClient } from "./host/authoringActions";
import { createAuthoringMediaPreviewClient } from "./host/authoringMediaPreview";
import { createAuthoringMediaSourceLeaseClient } from "./host/authoringMediaSourceLease";
import { createOutputClient } from "./host/authoringOutputActions";
import { createAuthoringOutputCapabilityClient } from "./host/authoringOutputCapabilityClient";
import { createOutputPreview } from "./host/authoringOutputPreview";
import { createBuildProvenanceClient } from "./host/buildProvenanceClient";
import { createComfyPromptHistoryClient } from "./host/comfyPromptHistory";
import { createDurationResolutionClient } from "./host/durationResolutionClient";
import { createGenerationSequenceDriver } from "./host/generationSequence";
import { createInputGeometryClient } from "./host/inputGeometry";
import { createMediaRuntimeClient } from "./host/mediaRuntimeClient";
import { createWorkspaceStateClient } from "./host/workspaceStateClient";
import { createWorkspaceStateLifecycle } from "./lifecycle/workspaceStateSession";
import { createRetainedAssetsClient } from "./host/retainedAssetsClient";
import { createRetainedAssetsLifecycle } from "./lifecycle/retainedAssetsSession";
import { createProjectDocumentClient } from "./host/projectDocumentClient";
import { createProjectRecoveryClient } from "./host/projectRecoveryClient";
import { createProjectPersistenceSession } from "./lifecycle/projectPersistenceSession";
import {
  createBrowserManagedSequenceReattachStore,
  createManagedSequenceClient,
} from "./host/managedSequence";
import { createProductionActionClient } from "./host/productionActions";
import { createBrowserProductionDestinationStore } from "./host/productionDestination";
import { createProductionAuthoringImportClient } from "./host/productionAuthoringImportClient";
import { createProductionGenerationController } from "./host/productionGeneration";
import { createProductionMediaPreviewClient } from "./host/productionMediaPreview";
import { createProductionProposalDispatcher } from "./host/productionProposalDispatcher";
import { createProviderSettingsClient } from "./host/providerSettingsActions";
import { createSequenceCoordinatorClient } from "./host/sequenceCoordinator";
import { createSidebarActionClient } from "./host/sidebarActions";
import { createSidebarHost } from "./host/sidebarHost";
import { createLocaleStore } from "./i18n/localeStore";
import { createAppModeMachine } from "./lifecycle/appModeMachine";
import { createExtensionSetupLifecycle } from "./lifecycle/extensionSetup";
import { createAppModeInterpreter } from "./lifecycle/interpreter";
import { createMountController } from "./lifecycle/mountController";
import { createPageRegistry } from "./navigation/pageRegistry";
import { createFrontendPerformanceRecorder } from "./performance/performanceBudget";
import { createDurationResolutionController } from "./state/durationResolutionState";
import { managedJournal } from "./state/managedJournal";
import styles from "./styles/tokens.css?inline";
import { createRoot } from "react-dom/client";
import { probeExtensionRegistrar, probeFetchApi } from "./host/hostSeams";
import {
  createShellSession,
  type ShellActions,
  type ShellDeps,
  type ShellRuntime,
} from "./lifecycle/shellSession";
import { createAppModeSession } from "./lifecycle/appModeSession";
import { createAppModeCorrelation } from "./lifecycle/appModeCorrelation";
import { createProductionSession } from "./host/productionSession";
import { createAuthoringSession } from "./host/authoringSession";
import { createProviderSession } from "./host/providerSession";
import { createPresentationBinding } from "./lifecycle/presentationBinding";
import { createExtensionRegistration } from "./lifecycle/extensionRegistration";
import { createNleWorkspaceSession } from "./lifecycle/nleWorkspaceSession";
import { createMediaRuntimeSession } from "./lifecycle/mediaRuntimeSession";
import { createProductionPlanningClient } from "./host/productionPlanningActions";
import { createManagedQualificationClient } from "./host/managedQualificationActions";

export { createProductionPlanningClient };
export { planningSelectors } from "./contracts/productionPlanningCodec";
export { createProductionManagedChildResolver } from "./host/productionManagedChildResolver";
export { createManagedQualificationClient };
export { buildQualifiedManagedStartIntent } from "./host/managedQualificationActions";
export { createProductionActionClient };

const session = createShellSession();

const mount = createMountController({ createRoot, styles });

const entrySetup = createExtensionSetupLifecycle();

const localeStore = createLocaleStore();

const pageRegistry = createPageRegistry();

const performanceRecorder = createFrontendPerformanceRecorder();

const host = createSidebarHost({ app, api, performanceRecorder });

const fetchApiProbe = probeFetchApi(api);
const fetchApi =
  fetchApiProbe.status === "ready"
    ? fetchApiProbe.value
    : (_path: string, _init: RequestInit) =>
        Promise.reject(new Error(fetchApiProbe.reason));

const actions = createSidebarActionClient({
  fetchApi,
  providerSessionHandle: () => session.providerSessionHandle,
});

const productionActions = createProductionActionClient({
  fetchApi,
});

const productionDestinations = createBrowserProductionDestinationStore();

const managedSequenceClient = createManagedSequenceClient({
  fetchApi: fetchApi as unknown as (
    route: string,
    init: RequestInit,
  ) => Promise<Response>,
});

const comfyPromptHistoryClient = createComfyPromptHistoryClient({
  fetchApi: fetchApi as unknown as (
    route: string,
    init: RequestInit,
  ) => Promise<Response>,
});

const managedSequenceReattachStore =
  createBrowserManagedSequenceReattachStore();

const sequenceCoordinator = createSequenceCoordinatorClient({
  fetchApi,
});

const authoringActions = createAuthoringActionClient({
  fetchApi,
});

const authoringMediaPreviewClient = createAuthoringMediaPreviewClient({
  fetchApi: fetchApi as unknown as (
    route: string,
    init: RequestInit,
  ) => Promise<Response>,
});

const authoringMediaSourceLeaseClient = createAuthoringMediaSourceLeaseClient({
  fetchApi: fetchApi as unknown as (
    route: string,
    init: RequestInit,
  ) => Promise<Response>,
});

// M25-16: the dedicated M25-29 import route and the M26 planning/readiness clients. Each is
// constructed once here and reached only through explicit user intent in the NLE session.
const productionAuthoringImportClient = createProductionAuthoringImportClient({
  fetchApi,
});

const productionPlanningClient = createProductionPlanningClient({ fetchApi });

const managedQualificationClient = createManagedQualificationClient({
  fetchApi,
});

// M25-16: the accepted M25-19 final-output leaf, mounted only inside the expanded workspace.
const authoringOutputCapabilityClient = createAuthoringOutputCapabilityClient({
  fetchApi,
});
const outputFetch = fetchApi as unknown as (
  route: string,
  init: RequestInit,
) => Promise<Response>;
const authoringOutputClient = createOutputClient(outputFetch);
const authoringOutputPreview = createOutputPreview(outputFetch);

// M25-33: media runtime status, setup and job routes; read on Settings open, a contextual
// runtime refusal or while a setup job runs.
const mediaRuntimeClient = createMediaRuntimeClient({ fetchApi });
const workspaceStateClient = createWorkspaceStateClient({ fetchApi });
const projectDocumentClient = createProjectDocumentClient({
  fetchApi: outputFetch,
});
const projectRecoveryClient = createProjectRecoveryClient({
  fetchApi: outputFetch,
});
const retainedAssetsClient = createRetainedAssetsClient({
  fetchApi: outputFetch,
});

const providerSettingsActions = createProviderSettingsClient({
  fetchApi,
  sessionHandle: () => session.providerSessionHandle,
});

const productionMediaPreviewClient = createProductionMediaPreviewClient({
  fetchApi: fetchApi as unknown as (
    route: string,
    init: RequestInit,
  ) => Promise<Response>,
});

const durationResolutionClient = createDurationResolutionClient({
  fetchApi,
});

const inputGeometryClient = createInputGeometryClient({
  fetchApi,
});

const buildProvenanceClient = createBuildProvenanceClient({
  fetchApi,
});

const durationResolutionController = createDurationResolutionController({
  resolve: (requestedSeconds, signal) =>
    durationResolutionClient.resolve(requestedSeconds, signal),
  changed: () => shellActions.renderCurrent(),
});

const productionProposalDispatcher = createProductionProposalDispatcher({
  send: (action, handle, current, request, signal) =>
    actions.sendProposal(action, handle, current, request, signal),
  changed: () => shellActions.renderCurrent(),
});

function beginOwnedGraphConfigure(): () => void {
  session.ownedGraphConfigureDepth += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    session.ownedGraphConfigureDepth = Math.max(
      0,
      session.ownedGraphConfigureDepth - 1,
    );
  };
}

const appModeController = createAppModeController(app, api, {
  beginOwnedGraphConfigure,
});

const shellActions = {} as ShellActions;

// IMPORTANT: AppModeController remains the sole aggregate host-effect seam until M23-28 splits it.
// Supplying a second queue executor here would duplicate the one authorized ComfyUI submission.
const appModeLifecycle = createAppModeInterpreter({
  machine: createAppModeMachine(),
  execute(effect) {
    if (effect.type === "detach")
      void shellActions.detachManagedSerialSequence();
  },
  onInvalidate() {
    void shellActions.detachManagedSerialSequence();
  },
  onDispose() {
    void shellActions.detachManagedSerialSequence();
  },
  inspect(entry) {
    if (entry.type === "event" && entry.name === "START") {
      session.diagnosticRun = entry.run;
      session.diagnosticRunsByAppRun.set(entry.run, entry.run);
      while (session.diagnosticRunsByAppRun.size > 8) {
        const oldest = session.diagnosticRunsByAppRun.keys().next().value;
        if (typeof oldest !== "number") break;
        session.diagnosticRunsByAppRun.delete(oldest);
      }
      session.lastDiagnosticState = "";
      managedJournal.beginRun(entry.run);
      return;
    }
    if (entry.type === "transition")
      managedJournal.recordTransition(entry.run, {
        event: entry.event,
        from: entry.from,
        to: entry.to,
      });
    else if (entry.type === "effect")
      managedJournal.recordEffect(entry.run, {
        name: entry.name,
        owner: entry.owner,
      });
  },
});

const productionGenerationController = createProductionGenerationController();

// IMPORTANT: the driver is constructed before the feature factories run, so it
// reaches `startAppMode` through the late-bound action table rather than a
// direct reference; the table is filled below, before any user action can start.
const generationSequenceDriver = createGenerationSequenceDriver(
  (inputs, options) => shellActions.startAppMode(inputs, options),
);

const deps: ShellDeps = Object.freeze({
  app,
  api,
  mount,
  entrySetup,
  localeStore,
  pageRegistry,
  performanceRecorder,
  host,
  actions,
  productionActions,
  productionDestinations,
  managedSequenceClient,
  managedSequenceReattachStore,
  comfyPromptHistoryClient,
  sequenceCoordinator,
  authoringActions,
  authoringMediaPreviewClient,
  providerSettingsActions,
  productionMediaPreviewClient,
  durationResolutionClient,
  inputGeometryClient,
  buildProvenanceClient,
  durationResolutionController,
  productionProposalDispatcher,
  appModeController,
  appModeLifecycle,
  generationSequenceDriver,
  productionGenerationController,
  productionAuthoringImportClient,
  productionPlanningClient,
  managedQualificationClient,
  authoringMediaSourceLeaseClient,
  authoringOutputCapabilityClient,
  authoringOutputClient,
  authoringOutputPreview,
  mediaRuntimeClient,
  workspaceStateClient,
  retainedAssetsClient,
  projectDocumentClient,
  projectRecoveryClient,
});

const runtime: ShellRuntime = Object.freeze({
  session,
  deps,
  actions: shellActions,
});

Object.assign(
  shellActions,
  createAppModeSession(runtime),
  createAppModeCorrelation(runtime),
  createProductionSession(runtime),
  createAuthoringSession(runtime),
  createProviderSession(runtime),
  createPresentationBinding(runtime),
  createNleWorkspaceSession(runtime),
  createMediaRuntimeSession(runtime),
  createWorkspaceStateLifecycle(runtime),
  createRetainedAssetsLifecycle(runtime),
  createProjectPersistenceSession(runtime),
);

// IMPORTANT: use the session that owns host events and detach fencing. A second runner
// here would lose terminal/artifact observations and could submit after sidebar removal.
export const managedProductionSession = Object.freeze({
  start: shellActions.startManagedSerialSequence,
  reattach: shellActions.reattachManagedSerialSequence,
  resume: shellActions.resumeManagedSerialSequence,
  detach: shellActions.detachManagedSerialSequence,
  cancel: shellActions.cancelManagedSerialSequence,
  settle: shellActions.settleManagedSerialSequence,
  snapshot: shellActions.managedSerialSequenceSnapshot,
});

const registrar = probeExtensionRegistrar(app);
if (registrar.status === "ready")
  registrar.value(createExtensionRegistration(runtime));
