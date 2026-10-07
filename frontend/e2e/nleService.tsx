import { createRoot, type Root } from "react-dom/client";
import { NlePlanningSection } from "../src/components/nle/NlePlanningSection";
import { NleSequencePanel } from "../src/components/nle/NleSequencePanel";
import type { NleWorkspaceBinding } from "../src/components/nle/nleWorkspaceBinding";
import { createNleWorkspaceSession } from "../src/lifecycle/nleWorkspaceSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import { initialNleWorkspaceState } from "../src/state/nleWorkspaceState";
import {
  createProductionPlanningClient,
  PRODUCTION_PLANNING_ROUTE,
} from "../src/host/productionPlanningActions";
import { createManagedQualificationClient } from "../src/host/managedQualificationActions";
import { createProductionActionClient } from "../src/host/productionActions";
import {
  createManagedSequenceClient,
  createManagedSequenceReattachStore,
} from "../src/host/managedSequenceClient";

// M25-16 evidence layer 2: this harness mounts the REAL shell session and the REAL
// NleSequencePanel, but every backend client is built over `fetch` against same-origin paths --
// there is no injected fake response anywhere in this file. Planning, readiness and Production
// calls issued from the mounted panel leave the page and reach the loopback application service
// started by frontend/e2e/serviceLoopback.ts. This is the one thing frontend/e2e/nleSequence.tsx
// deliberately is not: that harness's `fetchApi` throws on any host effect, by design, because it
// exercises the UI/codec layer against injected fixtures (evidence layer 1).

const fetchApi = (path: string, init: RequestInit) => fetch(path, init);

const productionPlanningClient = createProductionPlanningClient({ fetchApi });
const managedQualificationClient = createManagedQualificationClient({
  fetchApi,
});
const productionActionClient = createProductionActionClient({ fetchApi });
const managedSequenceClient = createManagedSequenceClient({ fetchApi });

// In-memory only: the B1 reattach pointer is irrelevant to this journey (it never starts a
// managed sequence), and nothing here may touch real browser storage.
const memoryStorage = new Map<string, string>();
const reattachStore = createManagedSequenceReattachStore({
  getItem: (key) => memoryStorage.get(key) ?? null,
  setItem: (key, value) => void memoryStorage.set(key, value),
  removeItem: (key) => void memoryStorage.delete(key),
});

const container = document.getElementById("root")!;
let root: Root | undefined;
let session = createShellSession();
let nle: ReturnType<typeof createNleWorkspaceSession>;
const events: string[] = [];
let requestCounter = 0;
// The seeded Context's own prompt, when the seed supplies it: what the Sidebar holds for a real
// Context and hands the planning section as the source for a storyboard script.
let contextPromptText: string | undefined;

// IMPORTANT: request ids are replay keys in the real service, and one loopback service outlives
// every page of a run. A counter that restarts with the page repeats an earlier page's id with a
// different payload, which the service refuses as a conflict; the page-load token keeps them apart.
const pageRun = Date.now().toString(36);

function nextId(prefix: string): string {
  requestCounter += 1;
  return `${prefix}.${pageRun}.${requestCounter}`;
}

function render(): void {
  if (!root) return;
  const binding = {
    locale: "en",
    state: session.nleWorkspace,
    production: session.productionState,
    contextAvailable: session.workspaceState.status !== "awaiting",
    actions: {
      setTargetSeconds: nle.nleSetTargetSeconds,
      setPolicy: nle.nleSetSegmentationPolicy,
      prepareContext: nle.nlePrepareContext,
      openStoryboardReview: nle.nleOpenStoryboardReview,
      setStoryboardRows: nle.nleSetStoryboardRows,
      admitStoryboard: nle.nleAdmitStoryboard,
      propose: nle.nlePropose,
      approveAndImportPlan: nle.nleApproveAndImportPlan,
      requestReadiness: nle.nleRequestReadiness,
      sequenceStartable: nle.nleSequenceStartable,
      // Starting a managed sequence is out of scope for this evidence layer: the loopback
      // service always refuses /prompt, so a real start attempt could only ever fail. Keeping
      // it absent here (rather than wired to a stub) means a stray click surfaces as a thrown
      // error instead of a silently-accepted no-op.
      startSequence: async () => {
        throw new Error(
          "managed sequence start is out of scope for this evidence layer",
        );
      },
      detachSequence: async () => {},
      reattachSequence: async () => {},
      resumeSequence: async () => {},
      cancelSequence: async () => {},
      retrySegment: async () => {},
      refreshSequence: nle.nleRefreshSequenceProjection,
      recoveryPointerPresent: nle.nleRecoveryPointerPresent,
      assembly: nle.nleAssembly,
      refreshProduction: nle.nleRefreshProduction,
    },
  } as unknown as NleWorkspaceBinding;
  const productionProjection =
    "projection" in binding.production
      ? binding.production.projection
      : undefined;
  root.render(
    <main aria-label="NLE service loopback workspace">
      {/* M25-63: planning lives in Production only. The harness keeps its flows by
          mounting Production's planning section beside the editor's Sequence tab, the
          composition the product has when the sidebar and the editor are both open. */}
      <section aria-label="Production">
        <NlePlanningSection
          locale={binding.locale}
          planning={binding.state.planning}
          readiness={binding.state.readiness}
          workspaceFingerprint={productionProjection?.workspaceFingerprint}
          enabled={
            productionProjection !== undefined && binding.contextAvailable
          }
          actions={binding.actions}
          contextPromptText={contextPromptText}
        />
      </section>
      <section aria-label="Clip editor">
        <NleSequencePanel binding={binding} />
      </section>
    </main>,
  );
}

function mount(): void {
  session = createShellSession();
  session.container = container;
  session.nleWorkspace = {
    ...initialNleWorkspaceState,
    surface: {
      ...initialNleWorkspaceState.surface,
      status: "expanded",
      bounds: { width: 1200, height: 800 },
    },
  };
  const actions = {
    renderCurrent: render,
    managedSerialSequenceSnapshot: () => ({
      parentSequenceId: null,
      parentState: null,
      activeSegmentId: null,
      activeQueuePromptId: null,
      failure: null,
    }),
    startManagedSerialSequence: async () => {
      events.push("sequence:start");
    },
    detachManagedSerialSequence: async () => {
      events.push("sequence:detach");
    },
    reattachManagedSerialSequence: async () => ({
      disposition: "recovery_unavailable_or_expired",
    }),
    resumeManagedSerialSequence: async () => {},
    cancelManagedSerialSequence: async () => {},
    retryManagedSerialSequence: async () => {},
    runAuthoringIntent: async () => {},
    selectPage: () => {},
    // Real read-through refresh: this is what nleApproveAndImportPlan calls after a successful
    // import, and what nleAssembly would call for an assembly action. It reaches the real
    // Production route over `fetch`, exactly like the rest of this harness.
    runProductionIntent: async ({ action }: { action: string }) => {
      events.push(`production:${action}`);
      if (action !== "read_projection") return;
      const current =
        session.productionState.status === "ready"
          ? session.productionState.projection
          : undefined;
      if (current === undefined) return;
      const result = await productionActionClient.send(
        nextId("production.read_projection"),
        "read_projection",
        { projection: current },
      );
      if (result.projection !== undefined)
        session.productionState = {
          status: "ready",
          projection: result.projection,
        };
      render();
    },
  };
  const ctx = {
    session,
    actions,
    deps: {
      app: { graph: { serialize: () => ({ nodes: [], links: [] }) } },
      api: { fetchApi },
      managedSequenceReattachStore: reattachStore,
      managedSequenceClient,
      productionPlanningClient,
      managedQualificationClient,
    },
  } as unknown as ShellRuntime;
  nle = createNleWorkspaceSession(ctx);
  root = createRoot(container);
  render();
}

mount();

window.nleServiceHarness = {
  /**
   * Real bootstrap: reads the loopback's seeded content-free context identity (the one boundary
   * that cannot be reached by a real /prompt-free browser -- see
   * scripts/m25_16_service_loopback.py's `_seed_sidebar_context`), then creates the Production
   * workspace through the real `/h3-context/v1/production/action` route.
   */
  async bootstrap(seedName: "default" | "multi_shot" = "default") {
    const contextResponse = await fetch(
      seedName === "multi_shot"
        ? "/__loopback/multi-shot-context"
        : "/__loopback/context",
    );
    if (!contextResponse.ok)
      throw new Error("loopback seed context read failed");
    const seed = (await contextResponse.json()) as {
      workspace_id: string;
      report_revision: number;
      report_fingerprint: string;
      prompt_text?: string;
    };
    contextPromptText = seed.prompt_text;
    session.workspaceState = {
      status: "ready",
      projection: {
        workspace_id: seed.workspace_id,
        report_revision: seed.report_revision,
        report_fingerprint: seed.report_fingerprint,
      },
    } as unknown as typeof session.workspaceState;
    const created = await productionActionClient.send(
      nextId("bootstrap.create_workspace"),
      "create_workspace_from_context",
      { contextWorkspaceHandle: seed.workspace_id },
    );
    if (created.projection === undefined)
      throw new Error("production workspace bootstrap failed");
    session.productionState = {
      status: "ready",
      projection: created.projection,
    };
    render();
  },

  /**
   * Deliberately raw: a same-origin POST to the real planning route bypassing the session's own
   * request bookkeeping, so a test can construct exact forged/stale selectors (a superseded
   * workspace revision, an already-consumed proposal id, two concurrent imports of one proposal)
   * that the accepted UI flow would never itself produce. It is still a genuine call into the
   * real, running `ProductionPlanningService` -- nothing here is a fixture.
   */
  async rawPlanningAction(
    action: string,
    payload: Record<string, unknown>,
    requestId: string,
  ) {
    const response = await fetch(PRODUCTION_PLANNING_ROUTE, {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        schema: "h3.context.production_planning.action.v1",
        request_id: requestId,
        action,
        payload,
      }),
    });
    const status = response.status;
    const body = status === 200 ? await response.json() : null;
    return { status, body };
  },

  /**
   * A brand-new Production workspace from the same seeded context. `assert_planning_context_source`
   * only matches a workspace whose segments still derive 1:1 from the raw context seed, which is
   * exactly true for a freshly created workspace and never again true once a plan has been
   * imported into it -- so each further planning round in the journey needs its own workspace
   * from this same real route, not a second round on the one already spent.
   */
  async rawCreateProductionWorkspace(contextWorkspaceHandle: string) {
    const created = await productionActionClient.send(
      nextId("round.create_workspace"),
      "create_workspace_from_context",
      { contextWorkspaceHandle },
    );
    if (created.projection === undefined)
      throw new Error("production workspace creation failed");
    return {
      workspaceHandle: created.projection.workspaceHandle,
      workspaceRevision: created.projection.workspaceRevision,
      workspaceFingerprint: created.projection.workspaceFingerprint,
    };
  },

  snapshot() {
    return {
      events: [...events],
      planning: session.nleWorkspace.planning,
      readiness: session.nleWorkspace.readiness,
      production: session.productionState,
      workspace: session.workspaceState,
    };
  },
};

declare global {
  interface Window {
    nleServiceHarness: {
      bootstrap(seedName?: "default" | "multi_shot"): Promise<void>;
      rawPlanningAction(
        action: string,
        payload: Record<string, unknown>,
        requestId: string,
      ): Promise<{ status: number; body: unknown }>;
      rawCreateProductionWorkspace(contextWorkspaceHandle: string): Promise<{
        workspaceHandle: string;
        workspaceRevision: number;
        workspaceFingerprint: string;
      }>;
      snapshot(): {
        events: string[];
        planning: unknown;
        readiness: unknown;
        production: unknown;
        workspace: unknown;
      };
    };
  }
}
