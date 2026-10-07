import { describe, expect, it, vi } from "vitest";

import {
  createSidebarHost,
  isUnverifiedModelFreeProjection,
  projectionFromOutput,
} from "../src/host/sidebarHost";
import { canonicalStringFingerprint } from "../src/contracts/canonicalFingerprint";
import { createFrontendPerformanceRecorder } from "../src/performance/performanceBudget";
import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";
import { generationSequenceWire } from "./generationSequenceFixture";
import {
  fixtureBackedSidebarApi,
  fixtureBackedSidebarDependencies,
  fixtureBackedSidebarHost,
} from "./support/hostSeamTestDouble";

const fingerprint = canonicalStringFingerprint(
  validSidebarWorkspace.prompt_text,
);
const canonicalGraphTypes = [
  "comfyui_h3_context.H3Context.Request",
  "comfyui_h3_context.H3Context.Plan",
  "comfyui_h3_context.H3Context.Compiler",
  "comfyui_h3_context.H3Context.Validator",
  "comfyui_h3_context.H3Context.NativeH3Adapter",
  "comfyui_h3_context.H3Context.Preview",
  "MiniMaxH3ImageToVideo",
];

function canonicalGraph(shellId = 8): Record<string, unknown> {
  return {
    nodes: [
      ...canonicalGraphTypes.map((type, index) => ({ id: index + 10, type })),
      { id: shellId, type: "comfyui_h3_context.H3Context.ProductShell" },
    ],
  };
}

function executedOutput(executionNodeId: string): Record<string, unknown[]> {
  const projection = {
    schema: "h3.context.product.shell.v1",
    product_scope: "MANUAL_ONLY_SCOPED",
    qualification_plan_fingerprint: fingerprint,
    report_id: "report-1",
    report_revision: 1,
    report_fingerprint: fingerprint,
    prompt_fingerprint: fingerprint,
    correlation: {
      prompt_id: "prompt-1",
      execution_node_id: executionNodeId,
    },
    task_mode: "ref2va",
    profile: "h3_full_reference",
    host: {
      node_api: "V1_ONLY",
      core_version: "0.32.0",
      core_revision: "b323a345bbbfb2f3a95b5b73b68eb7919a26515e",
      frontend_version: "1.48.7",
      frontend_revision: "6d6af63c00f132cd25dc29307fc56bd2c094fa22",
    },
    native_node_id: "MiniMaxH3ReferenceToVideo",
    prompt_export_ready: true,
    native_queue_ready: true,
    assisted_ready: false,
    readiness_reason: "manual_only_scoped",
    field_ids: ["h3.comfyui_h3_context_h3context_productshell.output.prompt"],
    bindings: [],
    limitations: ["visual.audio.live.profiles.unqualified"],
    assisted_authoring: {
      available: true,
      selected: false,
      ready: false,
      authorized_for_this_action: false,
      defaulted: false,
    },
  };
  const output: Record<string, unknown[]> = Object.fromEntries(
    Object.entries(projection).map(([key, value]) => [key, [value]]),
  );
  output.sidebar_workspace = [
    {
      ...validSidebarWorkspace,
      report_fingerprint: fingerprint,
      prompt_fingerprint: fingerprint,
      base_prompt_fingerprint: fingerprint,
      correlation: projection.correlation,
      bindings: [],
      reference_candidates: [],
      proposal: {
        ...validSidebarWorkspace.proposal,
        base_prompt_fingerprint: fingerprint,
        current_prompt_fingerprint: fingerprint,
      },
    },
  ];
  return output;
}

function transactionTransparency(executionNodeId: string) {
  return {
    schema: "h3.context.transaction_transparency.v1",
    workspace_id: "workspace.1",
    workspace_revision: 2,
    workspace_fingerprint: `sha256:${"a".repeat(64)}`,
    recompute_plan_fingerprint: `sha256:${"b".repeat(64)}`,
    correlation: { prompt_id: "prompt-1", execution_node_id: executionNodeId },
    selection_safe: true,
    requires_full_recompute: false,
    mandatory_segment_ids: ["segment.1"],
    requested_segment_ids: ["segment.1"],
    missing_required_segment_ids: [],
    decisions: [
      {
        segment_id: "segment.1",
        disposition: "dirty_self",
        reason_codes: ["producer_fingerprint_changed"],
        triggering_segment_ids: ["segment.1"],
      },
    ],
    transaction: {
      transaction_id: "transaction.1",
      transaction_fingerprint: `sha256:${"c".repeat(64)}`,
      attempt: 1,
      state: "prepared",
      graph_fingerprint: `sha256:${"d".repeat(64)}`,
      compiled_prompt_fingerprint: `sha256:${"e".repeat(64)}`,
      queue_prompt_id: null,
      host_owner_id: null,
      result_fingerprint: null,
      cancellation_requested: false,
    },
    actions: {
      confirm_native_queue: true,
      inspect_queue_history: false,
      return_to_native: true,
      rerun: false,
    },
    guidance: "review_before_native_queue",
  };
}

describe("sidebar host boundary", () => {
  it("accepts only a content-free exact-correlated semantic review handle", () => {
    const output = executedOutput("17");
    const semanticHandle = {
      schema: "h3.context.semantic_proposal_review_handle.v1",
      review_id: `review_${"r".repeat(32)}`,
      transaction_fingerprint: `sha256:${"a".repeat(64)}`,
      workspace_fingerprint: `sha256:${"b".repeat(64)}`,
      report_fingerprint: fingerprint,
      correlation: { prompt_id: "prompt-1", execution_node_id: "17" },
      available: true,
      reason: "review_available",
    };
    output.semantic_proposal_review = [semanticHandle];
    const handle = projectionFromOutput(output).semanticProposalReview;
    expect(handle?.reason).toBe("review_available");
    expect(JSON.stringify(handle)).not.toContain("groups");
    expect(JSON.stringify(handle)).not.toContain("summary");
    output.semantic_proposal_review = [
      { ...semanticHandle, proposal: "private" },
    ];
    expect(() => projectionFromOutput(output)).toThrow(/not closed/);
  });

  it("accepts an optional exact-correlated transaction payload and preserves legacy absence", () => {
    expect(
      projectionFromOutput(executedOutput("17")).transactionTransparency,
    ).toBeUndefined();
    const output = executedOutput("17");
    output.transaction_transparency = [transactionTransparency("17")];
    expect(projectionFromOutput(output).transactionTransparency).toMatchObject({
      transaction: { state: "prepared" },
    });
    output.transaction_transparency = [transactionTransparency("foreign")];
    expect(() => projectionFromOutput(output)).toThrow(/correlation drifted/);
  });
  it("accepts only an exact-correlated backend generation sequence", () => {
    const output = executedOutput("17");
    output.generation_sequence = [
      {
        ...generationSequenceWire(),
        correlation: { prompt_id: "prompt-1", execution_node_id: "17" },
      },
    ];
    expect(projectionFromOutput(output).generationSequence).toMatchObject({
      eligible_commands: [{ job_id: "job.1" }],
    });
    const foreign = generationSequenceWire();
    output.generation_sequence = [foreign];
    expect(() => projectionFromOutput(output)).toThrow(/correlation drifted/);
  });
  it("rejects model-free projections without an App Mode provenance owner", () => {
    const context = {
      source: "executed" as const,
      inspection: {
        status: "missing" as const,
        reason: "missing_native_h3_core",
        anchors: [
          {
            executionId: "1",
            nodeId: "comfyui_h3_context.H3Context.ProductShell" as const,
          },
        ],
        nodeCount: 1,
      },
    };
    expect(
      isUnverifiedModelFreeProjection(context, undefined, undefined, "1"),
    ).toBe(true);
    expect(
      isUnverifiedModelFreeProjection(
        context,
        7,
        { run: 7, executionNodeId: "1" },
        "1",
      ),
    ).toBe(false);
    expect(
      isUnverifiedModelFreeProjection(
        { ...context, source: "graph" },
        undefined,
        undefined,
        "1",
      ),
    ).toBe(false);
  });

  it("coalesces graph event bursts and cancels the pending refresh on destroy", () => {
    const tabs: Array<{ id: string }> = [];
    const graphListeners = new Map<string, EventListener>();
    const queued: Array<() => void> = [];
    const cancel = vi.fn();
    const serialize = vi.fn(() => ({ nodes: [] }));
    const manager = {
      registerSidebarTab: vi.fn((tab: { id: string }) => tabs.push(tab)),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    const performanceRecorder = createFrontendPerformanceRecorder({
      now: () => 1,
    });
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: {
            serialize,
            events: {
              addEventListener: (type: string, listener: EventListener) =>
                graphListeners.set(type, listener),
              removeEventListener: (type: string) =>
                graphListeners.delete(type),
            },
          },
          extensionManager: manager,
        },
        api: {
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
        },
        scheduleGraphRefresh: (callback) => {
          queued.push(callback);
          return cancel;
        },
        performanceRecorder,
      }),
    );
    const onGraph = vi.fn();
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension: vi.fn(),
      onProjection: vi.fn(),
      onGraph,
    });
    expect(serialize).toHaveBeenCalledOnce();
    graphListeners.get("change")?.(new Event("change"));
    graphListeners.get("graphChanged")?.(new Event("graphChanged"));
    graphListeners.get("configured")?.(new Event("configured"));
    expect(queued).toHaveLength(1);
    queued[0]?.();
    expect(serialize).toHaveBeenCalledTimes(2);
    graphListeners.get("change")?.(new Event("change"));
    const late = queued[1];
    host.disposeExtension();
    expect(cancel).toHaveBeenCalledOnce();
    late?.();
    expect(serialize).toHaveBeenCalledTimes(2);
    expect(onGraph).toHaveBeenCalledTimes(2);
    expect(host.performanceReceipt()).toMatchObject({
      graph_event_count: 4,
      refresh_count: 2,
      coalesced_event_count: 3,
      cleanup_verified: true,
    });
  });

  it("registers exactly one custom tab and owns event/unregister cleanup", () => {
    const tabs: Array<{ id: string; title: string; tooltip: string }> = [];
    const listeners = new Map<string, EventListener>();
    const app = fixtureBackedSidebarHost({
      graph: { serialize: () => ({ nodes: [] }) },
      extensionManager: {
        registerSidebarTab: vi.fn(
          (tab: { id: string; title: string; tooltip: string }) =>
            tabs.push(tab),
        ),
        unregisterSidebarTab: vi.fn((id: string) =>
          tabs.splice(
            tabs.findIndex((tab) => tab.id === id),
            1,
          ),
        ),
        getSidebarTabs: () => tabs,
      },
    });
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({ app, api }),
    );
    const mountView = vi.fn();
    const unmountView = vi.fn();
    const disposeExtension = vi.fn();
    let launcherCopy = { title: "H3 Context", tooltip: "Inspect" };
    const registration = {
      launcherCopy: () => launcherCopy,
      mountView,
      unmountView,
      disposeExtension,
      onProjection: vi.fn(),
      onGraph: vi.fn(),
    };
    host.register(registration);
    host.register(registration);
    expect(tabs).toHaveLength(1);
    expect(tabs[0]?.id).toBe("h3-context");
    expect(tabs[0]?.tooltip).toBe("Inspect");
    launcherCopy = { title: "H3 Context", tooltip: "檢視產品狀態" };
    expect(tabs[0]?.tooltip).toBe("檢視產品狀態");
    expect(listeners.has("executed")).toBe(true);
    host.disposeExtension();
    host.disposeExtension();
    expect(tabs).toHaveLength(0);
    expect(listeners.size).toBe(0);
    expect(disposeExtension).toHaveBeenCalledOnce();
  });

  it("retains fallback ownership when the host cannot unregister the tab", () => {
    type RegisteredTab = { id: string };
    const tabs: RegisteredTab[] = [];
    const listeners = new Map<string, EventListener>();
    const registerSidebarTab = vi.fn((tab: RegisteredTab) => tabs.push(tab));
    const app = fixtureBackedSidebarHost({
      graph: { serialize: () => ({ nodes: [] }) },
      extensionManager: { registerSidebarTab },
    });
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({ app, api }),
    );
    const disposeExtension = vi.fn();
    const registration = {
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension,
      onProjection: vi.fn(),
      onGraph: vi.fn(),
    };

    host.register(registration);
    host.register(registration);
    expect(registerSidebarTab).toHaveBeenCalledOnce();
    expect(tabs).toHaveLength(1);
    expect(listeners.has("executed")).toBe(true);

    expect(() => host.disposeExtension()).toThrow(
      /^sidebar_tab_unregistration_unavailable$/,
    );
    expect(disposeExtension).toHaveBeenCalledOnce();
    expect(listeners.size).toBe(0);
    expect(tabs).toHaveLength(1);

    host.register(registration);
    expect(registerSidebarTab).toHaveBeenCalledOnce();
    expect(tabs).toHaveLength(1);
  });

  it("forwards only bounded prompt identity for all execution terminal events", () => {
    const tabs: Array<{ id: string }> = [];
    const listeners = new Map<string, EventListener>();
    const app = fixtureBackedSidebarHost({
      graph: { serialize: () => ({ nodes: [] }) },
      extensionManager: {
        registerSidebarTab: vi.fn((tab: { id: string }) => tabs.push(tab)),
        unregisterSidebarTab: vi.fn((id: string) => {
          const index = tabs.findIndex((tab) => tab.id === id);
          if (index >= 0) tabs.splice(index, 1);
        }),
        getSidebarTabs: () => tabs,
      },
    });
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const onExecutionTerminal = vi.fn();
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({ app, api }),
    );
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension: vi.fn(),
      onProjection: vi.fn(),
      onExecutionTerminal,
      onGraph: vi.fn(),
    });

    expect([...listeners.keys()].sort()).toEqual([
      "executed",
      "execution_error",
      "execution_interrupted",
      "execution_success",
      "reconnected",
      "reconnecting",
      "status",
    ]);
    listeners.get("execution_error")?.(
      new CustomEvent("execution_error", {
        detail: {
          prompt_id: "prompt-1",
          exception_message: "private host diagnostic",
          traceback: ["private traceback"],
          prompt: { private: true },
        },
      }),
    );
    listeners.get("execution_interrupted")?.(
      new CustomEvent("execution_interrupted", {
        detail: { prompt_id: "prompt-2", node_id: "private-node" },
      }),
    );
    listeners.get("execution_success")?.(
      new CustomEvent("execution_success", {
        detail: { prompt_id: "prompt-3", outputs: { private: true } },
      }),
    );
    expect(onExecutionTerminal.mock.calls).toEqual([
      [{ promptId: "prompt-1", kind: "error" }],
      [{ promptId: "prompt-2", kind: "interrupted" }],
      [{ promptId: "prompt-3", kind: "success" }],
    ]);

    for (const prompt_id of ["", "prompt/with/path", "x".repeat(257), 42])
      listeners.get("execution_error")?.(
        new CustomEvent("execution_error", { detail: { prompt_id } }),
      );
    expect(onExecutionTerminal).toHaveBeenCalledTimes(3);

    host.disposeExtension();
    expect(listeners.size).toBe(0);
  });

  it("forwards host socket availability transitions once each", () => {
    const tabs: Array<{ id: string }> = [];
    const listeners = new Map<string, EventListener>();
    const app = fixtureBackedSidebarHost({
      graph: { serialize: () => ({ nodes: [] }) },
      extensionManager: {
        registerSidebarTab: vi.fn((tab: { id: string }) => tabs.push(tab)),
        unregisterSidebarTab: vi.fn((id: string) => {
          const index = tabs.findIndex((tab) => tab.id === id);
          if (index >= 0) tabs.splice(index, 1);
        }),
        getSidebarTabs: () => tabs,
      },
    });
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const onHostAvailability = vi.fn();
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({ app, api }),
    );
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension: vi.fn(),
      onProjection: vi.fn(),
      onHostAvailability,
      onGraph: vi.fn(),
    });

    const emit = (type: string, detail: unknown): void => {
      listeners.get(type)?.(new CustomEvent(type, { detail }));
    };
    emit("status", { exec_info: { queue_remaining: 1 } });
    expect(onHostAvailability).not.toHaveBeenCalled();
    emit("status", null);
    emit("status", null);
    emit("reconnecting", undefined);
    emit("reconnected", undefined);
    emit("reconnected", undefined);
    expect(onHostAvailability.mock.calls).toEqual([
      [{ phase: "lost", previous: "available" }],
      [{ phase: "reconnecting", previous: "lost" }],
      [{ phase: "available", previous: "reconnecting" }],
    ]);

    host.disposeExtension();
    expect(listeners.size).toBe(0);
  });

  it("keeps a consumer failure inside the availability seam", () => {
    const tabs: Array<{ id: string }> = [];
    const listeners = new Map<string, EventListener>();
    const app = fixtureBackedSidebarHost({
      graph: { serialize: () => ({ nodes: [] }) },
      extensionManager: {
        registerSidebarTab: vi.fn((tab: { id: string }) => tabs.push(tab)),
        unregisterSidebarTab: vi.fn(),
        getSidebarTabs: () => tabs,
      },
    });
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({ app, api }),
    );
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension: vi.fn(),
      onProjection: vi.fn(),
      onHostAvailability: () => {
        throw new Error("consumer failure");
      },
      onGraph: vi.fn(),
    });

    expect(() =>
      listeners.get("status")?.(new CustomEvent("status", { detail: null })),
    ).not.toThrow();
  });

  it("fails closed when the supported sidebar seam is absent", () => {
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: { graph: { serialize: () => ({ nodes: [] }) } },
        api: {},
      }),
    );
    expect(() =>
      host.register({
        launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
        mountView: vi.fn(),
        unmountView: vi.fn(),
        disposeExtension: vi.fn(),
        onProjection: vi.fn(),
        onGraph: vi.fn(),
      }),
    ).toThrow(/native nodes remain available/);
  });

  it("rolls back failed registration and permits a clean retry", () => {
    const tabs: Array<{ id: string }> = [];
    const listeners = new Map<string, EventListener>();
    let failRegistration = true;
    const manager = {
      registerSidebarTab: vi.fn((tab: { id: string }) => {
        if (failRegistration) throw new Error("tab registration failed");
        tabs.push(tab);
      }),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: { serialize: () => ({ nodes: [] }) },
          extensionManager: manager,
        },
        api,
      }),
    );
    const next = {
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension: vi.fn(),
      onProjection: vi.fn(),
      onGraph: vi.fn(),
    };
    expect(() => host.register(next)).toThrow(/tab registration failed/);
    expect(listeners.size).toBe(0);
    expect(tabs).toHaveLength(0);
    failRegistration = false;
    host.register(next);
    expect(tabs).toHaveLength(1);
    expect(listeners.has("executed")).toBe(true);
    host.disposeExtension();
  });

  it("continues all cleanup after one removal fails and retains the first failure", () => {
    const tabs: Array<{ id: string }> = [];
    const listeners = new Map<string, EventListener>();
    let failRemoval = true;
    const manager = {
      registerSidebarTab: vi.fn((tab: { id: string }) => tabs.push(tab)),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => {
        listeners.delete(type);
        if (failRemoval) {
          failRemoval = false;
          throw new Error("listener removal failed");
        }
      }),
    });
    const destroy = vi.fn();
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: { serialize: () => ({ nodes: [] }) },
          extensionManager: manager,
        },
        api,
      }),
    );
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension: destroy,
      onProjection: vi.fn(),
      onGraph: vi.fn(),
    });
    expect(() => host.disposeExtension()).toThrow(/listener removal failed/);
    expect(destroy).toHaveBeenCalledOnce();
    expect(manager.unregisterSidebarTab).toHaveBeenCalledWith("h3-context");
    expect(tabs).toHaveLength(0);
    expect(listeners.size).toBe(0);
  });

  it("unmounts the same-page view while retaining exactly one launcher", () => {
    type RegisteredTab = { id: string; destroy(): void };
    const tabs: RegisteredTab[] = [];
    const listeners = new Map<string, EventListener>();
    const manager = {
      registerSidebarTab: vi.fn((tab: RegisteredTab) => tabs.push(tab)),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const unmountView = vi.fn();
    const disposeExtension = vi.fn();
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: { serialize: () => ({ nodes: [] }) },
          extensionManager: manager,
        },
        api,
      }),
    );
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView,
      disposeExtension,
      onProjection: vi.fn(),
      onGraph: vi.fn(),
    });
    tabs[0]?.destroy();
    expect(unmountView).toHaveBeenCalledOnce();
    expect(disposeExtension).not.toHaveBeenCalled();
    expect(listeners.has("executed")).toBe(true);
    expect(tabs).toHaveLength(1);
    host.disposeExtension();
    expect(disposeExtension).toHaveBeenCalledOnce();
    expect(listeners.size).toBe(0);
    expect(tabs).toHaveLength(0);
  });

  it("refreshes graph truth exactly once on reopen without doubling initial setup", () => {
    type RegisteredTab = {
      id: string;
      render(container: HTMLElement): void;
      destroy(): void;
    };
    const tabs: RegisteredTab[] = [];
    const initialContainer = document.createElement("div");
    const manager = {
      registerSidebarTab: vi.fn((tab: RegisteredTab) => {
        tabs.push(tab);
        tab.render(initialContainer);
      }),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    let graph: Record<string, unknown> = { nodes: [] };
    const serialize = vi.fn(() => graph);
    const mountView = vi.fn();
    const unmountView = vi.fn();
    const onGraph = vi.fn();
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: { serialize },
          extensionManager: manager,
        },
        api: {
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
        },
      }),
    );
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView,
      unmountView,
      disposeExtension: vi.fn(),
      onProjection: vi.fn(),
      onGraph,
    });

    expect(mountView).toHaveBeenCalledOnce();
    expect(serialize).toHaveBeenCalledOnce();
    expect(onGraph).toHaveBeenCalledOnce();

    tabs[0]?.destroy();
    graph = canonicalGraph();
    tabs[0]?.render(document.createElement("div"));
    expect(mountView).toHaveBeenCalledTimes(2);
    expect(unmountView).toHaveBeenCalledOnce();
    expect(serialize).toHaveBeenCalledTimes(2);
    expect(onGraph).toHaveBeenCalledTimes(2);
    expect(onGraph).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "ready" }),
      "remount",
    );
    expect(host.performanceReceipt().refresh_count).toBe(2);

    const disposedTab = tabs[0];
    host.disposeExtension();
    expect(() => disposedTab?.render(document.createElement("div"))).toThrow(
      /no longer active/,
    );
    expect(mountView).toHaveBeenCalledTimes(2);
    expect(serialize).toHaveBeenCalledTimes(2);
  });

  it("treats a manager close during registration as view-only cleanup", () => {
    type RegisteredTab = { id: string; destroy(): void };
    const tabs: RegisteredTab[] = [];
    const listeners = new Map<string, EventListener>();
    let destroyDuringRegistration = true;
    const manager = {
      registerSidebarTab: vi.fn((tab: RegisteredTab) => {
        tabs.push(tab);
        if (destroyDuringRegistration) tab.destroy();
      }),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const unmountView = vi.fn();
    const disposeExtension = vi.fn();
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: { serialize: () => ({ nodes: [] }) },
          extensionManager: manager,
        },
        api,
      }),
    );
    const next = {
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView,
      disposeExtension,
      onProjection: vi.fn(),
      onGraph: vi.fn(),
    };

    host.register(next);
    expect(tabs).toHaveLength(1);
    expect(listeners.has("executed")).toBe(true);
    expect(unmountView).toHaveBeenCalledOnce();
    expect(disposeExtension).not.toHaveBeenCalled();

    host.disposeExtension();
    expect(disposeExtension).toHaveBeenCalledOnce();
    expect(tabs).toHaveLength(0);
    expect(listeners.size).toBe(0);

    destroyDuringRegistration = false;
    host.register(next);
    expect(tabs).toHaveLength(1);
    expect(listeners.has("executed")).toBe(true);
    host.disposeExtension();
  });

  it("rolls back listeners when onGraph destroys the host during setup", () => {
    type RegisteredTab = { id: string; destroy(): void };
    const tabs: RegisteredTab[] = [];
    const listeners = new Map<string, EventListener>();
    const manager = {
      registerSidebarTab: vi.fn((tab: RegisteredTab) => tabs.push(tab)),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: { serialize: () => ({ nodes: [] }) },
          extensionManager: manager,
        },
        api,
      }),
    );
    const destroy = vi.fn();

    expect(() =>
      host.register({
        launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
        mountView: vi.fn(),
        unmountView: vi.fn(),
        disposeExtension: destroy,
        onProjection: vi.fn(),
        onGraph: vi.fn(() => host.disposeExtension()),
      }),
    ).toThrow(/destroyed during setup/);
    expect(tabs).toHaveLength(0);
    expect(listeners.size).toBe(0);
    expect(destroy).toHaveBeenCalledOnce();
  });

  it("joins a genuine Subgraph execution by its composed internal node ID", () => {
    type RegisteredTab = { id: string; destroy(): void };
    const tabs: RegisteredTab[] = [];
    const listeners = new Map<string, EventListener>();
    const manager = {
      registerSidebarTab: vi.fn((tab: RegisteredTab) => tabs.push(tab)),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const onProjection = vi.fn();
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: {
            serialize: () => ({
              nodes: [
                ...canonicalGraphTypes.map((type, index) => ({
                  id: index + 10,
                  type,
                })),
                { id: 1, type: "definition-1" },
              ],
              definitions: {
                subgraphs: [
                  {
                    id: "definition-1",
                    nodes: [
                      {
                        id: 7,
                        type: "comfyui_h3_context.H3Context.ProductShell",
                      },
                    ],
                  },
                ],
              },
            }),
          },
          extensionManager: manager,
        },
        api,
      }),
    );
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension: vi.fn(),
      onProjection,
      onGraph: vi.fn(),
    });

    listeners.get("executed")?.(
      new CustomEvent("executed", {
        detail: {
          node: "1:7",
          display_node: "1",
          output: executedOutput("1:7"),
        },
      }),
    );
    expect(onProjection).toHaveBeenCalledOnce();
    expect(onProjection.mock.calls[0]?.[1]).toMatchObject({
      schema: "h3.context.sidebar.workspace.v2",
      correlation: { execution_node_id: "1:7" },
    });
    host.disposeExtension();
  });

  it("rescans the public graph when execution wins the configure-event race", () => {
    type RegisteredTab = { id: string; destroy(): void };
    const tabs: RegisteredTab[] = [];
    const listeners = new Map<string, EventListener>();
    let graph: Record<string, unknown> = { nodes: [] };
    const manager = {
      registerSidebarTab: vi.fn((tab: RegisteredTab) => tabs.push(tab)),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const serialize = vi.fn(() => graph);
    const onProjection = vi.fn();
    const onGraph = vi.fn();
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: { serialize },
          extensionManager: manager,
        },
        api,
      }),
    );
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension: vi.fn(),
      onProjection,
      onGraph,
    });
    graph = canonicalGraph(1);

    listeners.get("executed")?.(
      new CustomEvent("executed", {
        detail: {
          node: "1",
          display_node: "1",
          output: executedOutput("1"),
        },
      }),
    );

    expect(serialize).toHaveBeenCalledTimes(2);
    expect(onProjection).toHaveBeenCalledOnce();
    host.disposeExtension();
  });

  it("accepts a correlated model-free projection after the host removes generation", () => {
    type RegisteredTab = { id: string; destroy(): void };
    const tabs: RegisteredTab[] = [];
    const listeners = new Map<string, EventListener>();
    let graph: Record<string, unknown> = { nodes: [] };
    const manager = {
      registerSidebarTab: vi.fn((tab: RegisteredTab) => tabs.push(tab)),
      unregisterSidebarTab: vi.fn((id: string) => {
        const index = tabs.findIndex((tab) => tab.id === id);
        if (index >= 0) tabs.splice(index, 1);
      }),
      getSidebarTabs: () => tabs,
    };
    const api = fixtureBackedSidebarApi({
      addEventListener: vi.fn((type: string, listener: EventListener) =>
        listeners.set(type, listener),
      ),
      removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    });
    const serialize = vi.fn(() => graph);
    const onProjection = vi.fn();
    const onGraph = vi.fn();
    const host = createSidebarHost(
      fixtureBackedSidebarDependencies({
        app: {
          graph: { serialize },
          extensionManager: manager,
        },
        api,
      }),
    );
    host.register({
      launcherCopy: () => ({ title: "H3 Context", tooltip: "Inspect" }),
      mountView: vi.fn(),
      unmountView: vi.fn(),
      disposeExtension: vi.fn(),
      onProjection,
      onGraph,
    });

    // The supported host removes its weight-backed generation node after a
    // model-free execution, leaving only the ProductShell execution anchor.
    graph = {
      nodes: [
        {
          id: 1,
          type: "comfyui_h3_context.H3Context.ProductShell",
        },
      ],
    };
    listeners.get("executed")?.(
      new CustomEvent("executed", {
        detail: {
          node: "1",
          display_node: "1",
          output: executedOutput("1"),
        },
      }),
    );

    expect(serialize).toHaveBeenCalledTimes(2);
    expect(onGraph).toHaveBeenLastCalledWith(
      expect.objectContaining({
        status: "missing",
        reason: "missing_native_h3_core",
      }),
      "executed",
    );
    expect(onProjection).toHaveBeenCalledOnce();
    expect(onProjection.mock.calls[0]?.[2]).toMatchObject({
      source: "executed",
      inspection: {
        status: "missing",
        reason: "missing_native_h3_core",
      },
    });
    host.disposeExtension();
  });
});
