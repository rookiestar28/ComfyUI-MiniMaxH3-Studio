import { createRoot, type Root } from "react-dom/client";
import { NlePlanningSection } from "../src/components/nle/NlePlanningSection";
import { NleSequencePanel } from "../src/components/nle/NleSequencePanel";
import type { NleWorkspaceBinding } from "../src/components/nle/nleWorkspaceBinding";
import { decodeProductionPlanningResponse } from "../src/contracts/productionPlanningCodec";
import { observeOwnedGraph } from "../src/host/ownedGraphIdentity";
import { createNleWorkspaceSession } from "../src/lifecycle/nleWorkspaceSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import { initialNleWorkspaceState } from "../src/state/nleWorkspaceState";
import { createManagedSequenceHarnessEngine } from "./managedSequence";
import {
  CONTEXT_HANDLE,
  fp,
  plan,
  planningProjection,
  proposedProjection,
  productionProjection,
  readyReadiness,
} from "../tests/support/nleSequenceFixture";
import { unavailableProductionAssemblyWire } from "../tests/support/productionAssemblyWire";

// Real NLE orchestration and serial runner; only model-free application/queue responses
// are injected. Recreate models a page reload and discards both the React root and the
// shell/runner instances; leaveView models a view leave and return, which keeps them.
const ownedReference = Object.freeze({
  nodeIds: Object.freeze(["1"]),
  linkIds: Object.freeze([]),
  anchorNodeId: "1",
  authoredWidgetNodeIds: Object.freeze([]),
});
const canvasGraph = () => ({
  nodes: [{ id: 1, type: "SyntheticAnchor", inputs: [], outputs: [] }],
  links: [],
});
const engine = createManagedSequenceHarnessEngine({
  ownedProjectionFingerprint: observeOwnedGraph(canvasGraph(), ownedReference)
    .fingerprint,
});
const events: string[] = [];
const container = document.getElementById("root")!;
let root: Root | undefined;
let session = createShellSession();
let nle: ReturnType<typeof createNleWorkspaceSession>;
let mounts = 0;
let assemblyState: "unavailable" | "ready" | "running" | "succeeded" =
  "unavailable";

function currentProduction() {
  const available = assemblyState !== "unavailable";
  return productionProjection(fp("a"), {
    reconstruction: {
      state: assemblyState === "succeeded" ? "complete" : "unavailable",
    },
    assembly: {
      ...unavailableProductionAssemblyWire(),
      state: assemblyState === "ready" ? "unavailable" : assemblyState,
      progress: {
        completed: assemblyState === "succeeded" ? 2 : 0,
        total: available ? 2 : 0,
      },
      capability_fingerprint: available ? fp("1") : null,
      managed_sequence_fingerprint: available ? fp("2") : null,
      artifact_receipt_fingerprints: available ? [fp("3"), fp("4")] : [],
      cut_boundary_receipt_fingerprints: available ? [fp("7")] : [],
      assembly_job_id:
        assemblyState === "running" || assemblyState === "succeeded"
          ? "assembly.1"
          : null,
      authorization_fingerprint:
        assemblyState === "running" || assemblyState === "succeeded"
          ? fp("5")
          : null,
      receipt_fingerprint: assemblyState === "succeeded" ? fp("6") : null,
      failure_code: available ? null : "media_runtime_not_authorized",
    },
    allowed_actions: [
      "read_projection",
      ...(assemblyState === "ready" ? ["assemble_sequence"] : []),
      ...(assemblyState === "running" ? ["cancel_assembly"] : []),
    ],
  });
}

function render() {
  if (!root) return;
  const binding = {
    locale: "en",
    state: session.nleWorkspace,
    production: session.productionState,
    contextAvailable: true,
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
      startSequence: nle.nleStartSequence,
      detachSequence: nle.nleDetachSequence,
      reattachSequence: nle.nleReattachSequence,
      resumeSequence: nle.nleResumeSequence,
      cancelSequence: nle.nleCancelSequence,
      retrySegment: nle.nleRetrySegment,
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
    <main aria-label="NLE sequence workspace">
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
        />
      </section>
      <section aria-label="Clip editor">
        <NleSequencePanel binding={binding} />
      </section>
    </main>,
  );
}

function mount() {
  mounts += 1;
  session = createShellSession();
  session.container = container;
  session.acceptedManagedIdentity = {
    workflowAuthority: engine.workflowAuthority,
    ownedReference,
    ownedProjectionFingerprint: observeOwnedGraph(canvasGraph(), ownedReference)
      .fingerprint,
  } as unknown as typeof session.acceptedManagedIdentity;
  session.nleWorkspace = {
    ...initialNleWorkspaceState,
    surface: {
      ...initialNleWorkspaceState.surface,
      status: "expanded",
      generation: mounts,
      bounds: { width: 1200, height: 800 },
    },
  };
  session.productionState = {
    status: "ready",
    projection: currentProduction(),
  };
  session.workspaceState = {
    status: "ready",
    projection: {
      workspace_id: CONTEXT_HANDLE,
      report_revision: 0,
      report_fingerprint: fp("a"),
    },
  } as unknown as typeof session.workspaceState;
  const actions = {
    renderCurrent: render,
    managedSerialSequenceSnapshot: () => ({
      ...engine.runner().snapshot(),
      failure: null,
    }),
    startManagedSerialSequence: async (
      intent: Parameters<ReturnType<typeof engine.runner>["start"]>[0],
    ) => {
      events.push("sequence:start");
      await engine.runner().start(intent);
    },
    detachManagedSerialSequence: async () => {
      events.push("sequence:detach");
      await engine.runner().detach();
    },
    reattachManagedSerialSequence: async () => {
      events.push("sequence:reattach");
      return engine.runner().reattach();
    },
    resumeManagedSerialSequence: async (
      identity: Parameters<ReturnType<typeof engine.runner>["resume"]>[0],
    ) => {
      events.push("sequence:resume");
      return engine.runner().resume(identity);
    },
    managedSerialCanvasLabel: () => engine.runner().canvasLabel(),
    runProductionIntent: async ({ action }: { action: string }) => {
      events.push(`production:${action}`);
      if (action === "assemble_sequence") assemblyState = "running";
      if (action === "read_projection" && assemblyState === "running")
        assemblyState = "succeeded";
      session.productionState = {
        status: "ready",
        projection: currentProduction(),
      };
      render();
    },
  };
  const ctx = {
    session,
    actions,
    deps: {
      app: {
        graph: { serialize: canvasGraph },
        // The workflow tab the engine's children were bound in stays the active open tab.
        extensionManager: {
          workflow: {
            activeWorkflow: engine.workflowAuthority,
            openWorkflows: [engine.workflowAuthority],
          },
        },
      },
      api: {
        fetchApi: async () => {
          throw new Error("unexpected_host_effect");
        },
      },
      managedSequenceReattachStore: engine.reattachStore,
      managedSequenceClient: engine.parentClient,
      productionPlanningClient: {
        async send(
          requestId: string,
          action: string,
          payload: Record<string, unknown>,
        ) {
          events.push(`planning:${action}`);
          const selectors = {
            request_id: requestId,
            target_seconds: session.nleWorkspace.planning.targetSeconds,
            policy: session.nleWorkspace.planning.policy,
          };
          if (action === "prepare_context")
            return decodeProductionPlanningResponse(
              planningProjection(selectors),
            );
          if (action === "admit_storyboard") {
            if (
              payload.source_kind === "user_reviewed_typed_rows" &&
              payload.user_reviewed !== true
            )
              throw new Error("review_required");
            return decodeProductionPlanningResponse(
              planningProjection({
                ...selectors,
                admission_id: "admission_owned",
              }),
            );
          }
          if (action === "propose")
            return decodeProductionPlanningResponse({
              ...proposedProjection(),
              ...selectors,
            });
          if (action === "import_plan")
            return decodeProductionPlanningResponse(
              plan({
                request_id: requestId,
                segment_ids: ["segment.1", "segment.2"],
                reconstruction_order: ["segment.1", "segment.2"],
                cut_boundary_receipts: [
                  {
                    schema: "h3.context.production_cut_boundary_receipt.v1",
                    predecessor_segment_id: "segment.1",
                    successor_segment_id: "segment.2",
                    boundary_milliseconds: 10000,
                    join_policy: "cut",
                    predecessor_artifact_required: false,
                  },
                ],
              }),
            );
          throw new Error("unexpected_planning_action");
        },
      },
      managedQualificationClient: {
        async send() {
          events.push("readiness:request");
          return readyReadiness();
        },
      },
    },
  } as unknown as ShellRuntime;
  nle = createNleWorkspaceSession(ctx);
  root = createRoot(container);
  render();
}

mount();
window.nleSequenceHarness = {
  snapshot: () => ({
    mounts,
    events: [...events],
    engine: engine.snapshot(),
    pointer: engine.reattachStore.read(),
    state: session.nleWorkspace.sequence.ui,
  }),
  async completeCurrent() {
    const prompt = engine.runner().snapshot().activeQueuePromptId;
    if (prompt === null) throw new Error("no_current_child");
    await engine.sendTerminalPair(prompt);
    // Backend truth only: the workspace itself must read the new Production projection.
    if (engine.runner().snapshot().parentState === "succeeded")
      assemblyState = "ready";
    await nle.nleRefreshSequenceProjection();
    render();
  },
  recreate() {
    nle.nleDisposeOverlay();
    root?.unmount();
    root = undefined;
    container.replaceChildren();
    engine.recreateRunner();
    mount();
  },
  leaveView() {
    nle.nleDisposeOverlay();
    root?.unmount();
    root = undefined;
    container.replaceChildren();
    mounts += 1;
    session.nleWorkspace = {
      ...session.nleWorkspace,
      surface: {
        ...session.nleWorkspace.surface,
        status: "expanded",
        generation: mounts,
        bounds: { width: 1200, height: 800 },
      },
    };
    root = createRoot(container);
    render();
  },
};

declare global {
  interface Window {
    nleSequenceHarness: {
      snapshot(): {
        mounts: number;
        events: string[];
        engine: ReturnType<typeof engine.snapshot>;
        pointer: ReturnType<typeof engine.reattachStore.read>;
        state: string;
      };
      completeCurrent(): Promise<void>;
      recreate(): void;
      leaveView(): void;
    };
  }
}
