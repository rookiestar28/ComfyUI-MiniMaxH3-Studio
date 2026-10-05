import {
  decodeProductShellProjection,
  type ProductShellProjection,
} from "../contracts/projectionCodecs";
import {
  decodeSidebarWorkspaceProjection,
  type SidebarWorkspaceProjection,
} from "../contracts/sidebarWorkspaceCodec";
import { canonicalStringFingerprint } from "../contracts/canonicalFingerprint";
import {
  decodeTransactionTransparencyProjection,
  type TransactionTransparencyProjection,
} from "../contracts/transactionTransparencyCodec";
import {
  decodeGenerationSequenceProjection,
  type GenerationSequenceProjection,
} from "../contracts/generationSequenceCodec";
import {
  decodeSemanticProposalReviewHandle,
  type SemanticProposalReviewHandle,
} from "../contracts/semanticProposalReviewCodec";
import {
  createGraphRefreshCoalescer,
  createFrontendPerformanceRecorder,
  type FrontendPerformanceRecorder,
  type OwnedSchedule,
} from "../performance/performanceBudget";
import {
  H3_SHELL_MANIFEST,
  inspectH3GraphAdmission,
  type GraphInspection,
} from "./graphAdapter";
import {
  HOST_AVAILABILITY_EVENT_NAMES,
  createHostAvailabilityTracker,
  type HostAvailabilityTransition,
} from "./hostAvailability";
import {
  probeApiEventTarget,
  probeGraphEvents,
  probeGraphSerializer,
  probeSidebarTabManager,
  type SeamRefusal,
} from "./hostSeams";

const tabId = "h3-context";
const SIDEBAR_UNREGISTER_SEAM_ABSENT: SeamRefusal =
  "sidebar_tab_unregistration_unavailable";
const projectionKeys = [
  "schema",
  "product_scope",
  "qualification_plan_fingerprint",
  "report_id",
  "report_revision",
  "report_fingerprint",
  "prompt_fingerprint",
  "correlation",
  "task_mode",
  "profile",
  "host",
  "native_node_id",
  "prompt_export_ready",
  "native_queue_ready",
  "assisted_ready",
  "readiness_reason",
  "field_ids",
  "bindings",
  "limitations",
  "assisted_authoring",
] as const;
type SidebarTab = {
  id: string;
  title: string;
  tooltip: string;
  icon: string;
  type: "custom";
  render(container: HTMLElement): void;
  destroy(): void;
};
type ExtensionManager = {
  registerSidebarTab?(tab: SidebarTab): void;
  unregisterSidebarTab?(id: string): void;
  getSidebarTabs?(): Array<{ id: string }>;
};
type EventSource = {
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
};
type HostApp = {
  graph?: { serialize?: () => unknown; events?: EventSource };
  extensionManager?: ExtensionManager;
};
type HostApi = Partial<EventSource>;
type GraphRefreshSource = "graph" | "executed" | "remount";
export type ExecutionTerminalKind = "error" | "interrupted" | "success";
export type ExecutionTerminalEvent = Readonly<{
  promptId: string;
  kind: ExecutionTerminalKind;
}>;
export type SaveVideoArtifactEvent = Readonly<{
  promptId: string;
  outputNodeId: string;
  locator: Readonly<{
    filename: string;
    subfolder: string;
    type: "output";
  }>;
}>;
export type ProjectionGraphContext = {
  inspection: GraphInspection;
  source: "graph" | "executed";
};
type Registration = {
  launcherCopy(): { title: string; tooltip: string };
  mountView(container: HTMLElement): void;
  unmountView(): void;
  disposeExtension(): void;
  onProjection(
    projection: ProductShellProjection,
    workspace: SidebarWorkspaceProjection,
    graphContext: ProjectionGraphContext,
    transactionTransparency?: TransactionTransparencyProjection,
    generationSequence?: GenerationSequenceProjection,
    semanticProposalReview?: SemanticProposalReviewHandle,
  ): void;
  onExecutionTerminal?(event: ExecutionTerminalEvent): void;
  onSaveVideoArtifact?(event: SaveVideoArtifactEvent): void;
  onHostAvailability?(transition: HostAvailabilityTransition): void;
  onGraph(inspection: GraphInspection, source?: GraphRefreshSource): void;
};
type SetupTransaction = {
  generation: number;
  registration: Registration;
  view: { mounted: boolean; mountedOnce: boolean };
  removals: Array<() => void>;
  destroyed: boolean;
};

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function boundedPromptId(value: unknown): string | undefined {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 256 ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value)
  )
    return undefined;
  return value;
}

/** Decode only stock SaveVideo's one-result PreviewVideo UI payload. */
export function saveVideoArtifactFromExecuted(
  detailValue: unknown,
): SaveVideoArtifactEvent | undefined {
  const detail = record(detailValue);
  const promptId = boundedPromptId(detail?.prompt_id);
  const outputNodeId = boundedPromptId(detail?.node);
  const output = record(detail?.output);
  if (
    promptId === undefined ||
    outputNodeId === undefined ||
    output === undefined ||
    Object.keys(output).some((key) => key !== "images" && key !== "animated") ||
    !Array.isArray(output.images) ||
    output.images.length !== 1 ||
    !Array.isArray(output.animated) ||
    output.animated.length !== 1 ||
    output.animated[0] !== true
  )
    return undefined;
  const saved = record(output.images[0]);
  if (
    saved === undefined ||
    Object.keys(saved).length !== 3 ||
    !Object.hasOwn(saved, "filename") ||
    !Object.hasOwn(saved, "subfolder") ||
    !Object.hasOwn(saved, "type") ||
    typeof saved.filename !== "string" ||
    saved.filename.length === 0 ||
    saved.filename.length > 255 ||
    typeof saved.subfolder !== "string" ||
    saved.subfolder.length > 512 ||
    saved.type !== "output"
  )
    return undefined;
  return Object.freeze({
    promptId,
    outputNodeId,
    locator: Object.freeze({
      filename: saved.filename,
      subfolder: saved.subfolder,
      type: "output" as const,
    }),
  });
}

export function projectionFromOutput(outputValue: unknown): {
  projection: ProductShellProjection;
  workspace: SidebarWorkspaceProjection;
  transactionTransparency?: TransactionTransparencyProjection;
  generationSequence?: GenerationSequenceProjection;
  semanticProposalReview?: SemanticProposalReviewHandle;
} {
  const output = record(outputValue);
  if (output === undefined) throw new Error("executed output is not an object");
  const wire: Record<string, unknown> = {};
  for (const key of projectionKeys) {
    const values = output[key];
    if (!Array.isArray(values) || values.length !== 1)
      throw new Error("executed output is not list-valued");
    wire[key] = values[0];
  }
  const projection = decodeProductShellProjection(wire);
  const workspaceValues = output.sidebar_workspace;
  if (!Array.isArray(workspaceValues) || workspaceValues.length !== 1)
    throw new Error("sidebar workspace is not list-valued");
  const workspace = decodeSidebarWorkspaceProjection(workspaceValues[0]);
  if (
    workspace.report_id !== projection.report_id ||
    workspace.report_revision !== projection.report_revision ||
    workspace.report_fingerprint !== projection.report_fingerprint ||
    workspace.prompt_fingerprint !== projection.prompt_fingerprint ||
    workspace.task_mode !== projection.task_mode ||
    workspace.profile !== projection.profile ||
    workspace.product_scope !== projection.product_scope ||
    workspace.assisted_authoring.available !==
      projection.assisted_authoring.available ||
    workspace.assisted_authoring.selected !==
      projection.assisted_authoring.selected ||
    workspace.assisted_authoring.ready !==
      projection.assisted_authoring.ready ||
    workspace.assisted_authoring.authorized_for_this_action !==
      projection.assisted_authoring.authorized_for_this_action ||
    workspace.assisted_authoring.defaulted !==
      projection.assisted_authoring.defaulted ||
    workspace.correlation.prompt_id !== projection.correlation.prompt_id ||
    workspace.correlation.execution_node_id !==
      projection.correlation.execution_node_id
  )
    throw new Error("workspace and product projection identities drifted");
  // CRITICAL: the backend-owned prompt hash must be independently checked
  // against the unredacted workspace text; comparing two projection fields is
  // not sufficient to reject a foreign prompt with reused queue identity.
  if (
    !workspace.prompt_text_redacted &&
    canonicalStringFingerprint(workspace.prompt_text) !==
      workspace.prompt_fingerprint
  )
    throw new Error("workspace prompt fingerprint is not text-derived");
  const transactionValues = output.transaction_transparency;
  let transactionTransparency: TransactionTransparencyProjection | undefined;
  if (transactionValues !== undefined) {
    if (!Array.isArray(transactionValues) || transactionValues.length !== 1)
      throw new Error("transaction transparency is not list-valued");
    transactionTransparency = decodeTransactionTransparencyProjection(
      transactionValues[0],
    );
    if (
      transactionTransparency.correlation.prompt_id !==
        projection.correlation.prompt_id ||
      transactionTransparency.correlation.execution_node_id !==
        projection.correlation.execution_node_id
    )
      throw new Error("transaction transparency correlation drifted");
  }
  const sequenceValues = output.generation_sequence;
  let generationSequence: GenerationSequenceProjection | undefined;
  if (sequenceValues !== undefined) {
    if (!Array.isArray(sequenceValues) || sequenceValues.length !== 1)
      throw new Error("generation sequence is not list-valued");
    generationSequence = decodeGenerationSequenceProjection(sequenceValues[0]);
    if (
      generationSequence.correlation.prompt_id !==
        projection.correlation.prompt_id ||
      generationSequence.correlation.execution_node_id !==
        projection.correlation.execution_node_id
    )
      throw new Error("generation sequence correlation drifted");
  }
  const reviewValues = output.semantic_proposal_review;
  let semanticProposalReview: SemanticProposalReviewHandle | undefined;
  if (reviewValues !== undefined) {
    if (!Array.isArray(reviewValues) || reviewValues.length !== 1)
      throw new Error("semantic proposal review is not list-valued");
    semanticProposalReview = decodeSemanticProposalReviewHandle(
      reviewValues[0],
    );
    if (
      semanticProposalReview.report_fingerprint !==
        projection.report_fingerprint ||
      semanticProposalReview.correlation.prompt_id !==
        projection.correlation.prompt_id ||
      semanticProposalReview.correlation.execution_node_id !==
        projection.correlation.execution_node_id
    )
      throw new Error("semantic proposal review correlation drifted");
  }
  return {
    projection,
    workspace,
    transactionTransparency,
    generationSequence,
    semanticProposalReview,
  };
}

export function isUnverifiedModelFreeProjection(
  context: ProjectionGraphContext | undefined,
  pendingRun: number | undefined,
  executedRefresh: { run: number; executionNodeId: string } | undefined,
  executionNodeId: string,
): boolean {
  if (
    context?.source !== "executed" ||
    context.inspection.status !== "missing" ||
    context.inspection.reason !== "missing_native_h3_core"
  )
    return false;
  return (
    pendingRun === undefined ||
    executedRefresh === undefined ||
    executedRefresh.run !== pendingRun ||
    executedRefresh.executionNodeId !== executionNodeId
  );
}

function graphMatchesProjection(
  inspection: GraphInspection,
  executionNodeId: string,
): boolean {
  if (inspection.anchors[0]?.executionId !== executionNodeId) return false;
  // The model-free host lane removes the weight-backed generation node after
  // execution. The result remains correlated to the ProductShell anchor even
  // though the rescan is incomplete and must not become a queue target.
  return (
    inspection.status === "ready" ||
    (inspection.status === "missing" &&
      inspection.reason === "missing_native_h3_core")
  );
}

function recordHostProjectionTrace(
  stage: string,
  detail: Record<string, string | number | boolean | undefined>,
): void {
  const sink = (
    globalThis as typeof globalThis & {
      __h3HostProjectionTrace?: Array<Record<string, unknown>>;
    }
  ).__h3HostProjectionTrace;
  if (Array.isArray(sink)) sink.push({ stage, ...detail });
}

export function createSidebarHost(dependencies: {
  app: HostApp;
  api: HostApi;
  scheduleGraphRefresh?: OwnedSchedule;
  performanceRecorder?: FrontendPerformanceRecorder;
}) {
  const mountRegistrationView = (
    owned: Registration,
    view: SetupTransaction["view"],
    container: HTMLElement,
  ): void => {
    const remount = view.mountedOnce && !view.mounted;
    owned.mountView(container);
    view.mounted = true;
    view.mountedOnce = true;
    if (remount) {
      if (registration !== owned)
        throw new Error("sidebar extension is no longer active");
      refreshGraph(false, "remount");
    }
  };
  const unmountRegistrationView = (
    owned: Registration,
    view: SetupTransaction["view"],
  ): void => {
    owned.unmountView();
    view.mounted = false;
  };
  const disposeRegistration = (owned: Registration): void => {
    owned.disposeExtension();
  };
  const performanceRecorder =
    dependencies.performanceRecorder ?? createFrontendPerformanceRecorder();
  let registration: Registration | undefined;
  let latestGraph: GraphInspection = {
    status: "incompatible",
    anchors: [],
    nodeCount: 0,
    reason: "not_inspected",
  };
  let latestGraphFailure: string | undefined;
  const removals: Array<() => void> = [];
  let tabRegistered = false;
  let destroying = false;
  let generation = 0;
  let activeSetup: SetupTransaction | undefined;

  const refreshGraph = (
    fromGraphEvent = false,
    source: GraphRefreshSource = "graph",
  ): void => {
    performanceRecorder.recordRefresh(fromGraphEvent);
    performanceRecorder.measure("refresh", () => {
      const current = registration;
      if (current === undefined) return;
      const serializer = probeGraphSerializer(dependencies.app);
      if (serializer.status !== "ready") {
        latestGraph = {
          status: "incompatible",
          anchors: [],
          nodeCount: 0,
          reason: "missing_serialize",
        };
        current.onGraph(latestGraph, source);
        return;
      }
      try {
        latestGraph = inspectH3GraphAdmission(
          serializer.value(),
          H3_SHELL_MANIFEST,
        );
        latestGraphFailure = undefined;
      } catch (error) {
        latestGraphFailure =
          error instanceof Error
            ? `${error.name}:${error.message}`.slice(0, 160)
            : typeof error;
        latestGraph = {
          status: "incompatible",
          anchors: [],
          nodeCount: 0,
          reason: "invalid_serialized_graph",
        };
      }
      recordHostProjectionTrace("graph", {
        source,
        graph_status: latestGraph.status,
        graph_reason: latestGraph.reason,
        node_count: latestGraph.nodeCount,
        graph_failure: latestGraphFailure,
      });
      current.onGraph(latestGraph, source);
    });
  };

  const listen = (
    source: HostApi | EventSource,
    type: string,
    listener: EventListener,
    ownedRemovals: Array<() => void>,
  ): void => {
    if (
      typeof source.addEventListener !== "function" ||
      typeof source.removeEventListener !== "function"
    )
      return;
    // CRITICAL: own the inverse before add; a throwing host call may still have side effects.
    ownedRemovals.push(() => source.removeEventListener?.(type, listener));
    source.addEventListener(type, listener);
  };

  const disposeExtension = (): void => {
    if (destroying) return;
    destroying = true;
    generation += 1;
    const setup = activeSetup;
    activeSetup = undefined;
    if (setup !== undefined) setup.destroyed = true;
    const current = registration;
    registration = undefined;
    let firstFailure: unknown;
    let failed = false;
    const attempt = (cleanup: () => void): void => {
      try {
        cleanup();
      } catch (error) {
        if (!failed) {
          failed = true;
          firstFailure = error;
        }
      }
    };
    try {
      if (setup !== undefined) {
        while (setup.removals.length > 0) {
          const removal = setup.removals.pop();
          if (removal !== undefined) attempt(removal);
        }
      }
      while (removals.length > 0) {
        const removal = removals.pop();
        if (removal !== undefined) attempt(removal);
      }
      const ownedRegistration = current ?? setup?.registration;
      if (ownedRegistration !== undefined)
        attempt(() => disposeRegistration(ownedRegistration));
      const tabs = probeSidebarTabManager(dependencies.app);
      if (tabs.status === "ready" && tabRegistered) {
        // IMPORTANT (M23-28): `getSidebarTabs` is observed-but-undocumented. When
        // it is absent the extension-owned token `tabRegistered` is the only
        // authority; never invent an enumeration from host internals.
        let shouldUnregister = true;
        if (tabs.value.enumerate !== undefined)
          try {
            shouldUnregister = tabs.value
              .enumerate()
              .some((tab) => tab.id === tabId);
          } catch (error) {
            if (!failed) {
              failed = true;
              firstFailure = error;
            }
          }
        tabRegistered = false;
        if (shouldUnregister) {
          if (tabs.value.unregister === undefined) {
            // Named disposal refusal: the tab stays registered and the caller
            // learns why, instead of a TypeError from calling an absent member.
            tabRegistered = true;
            if (!failed) {
              failed = true;
              firstFailure = new Error(SIDEBAR_UNREGISTER_SEAM_ABSENT);
            }
          } else
            try {
              tabs.value.unregister(tabId);
            } catch (error) {
              if (!failed) {
                failed = true;
                firstFailure = error;
              }
              try {
                tabRegistered =
                  tabs.value.enumerate === undefined
                    ? true
                    : tabs.value.enumerate().some((tab) => tab.id === tabId);
              } catch {
                tabRegistered = true;
              }
            }
        }
      }
    } finally {
      destroying = false;
      performanceRecorder.destroy();
    }
    if (failed) throw firstFailure;
  };

  return {
    register(next: Registration): void {
      // IMPORTANT (M23-28): an absent unregister seam leaves the host tab alive. Retain the
      // extension-owned token as an idempotence guard or a later register call duplicates it.
      if (
        registration !== undefined ||
        activeSetup !== undefined ||
        tabRegistered
      )
        return;
      const tabs = probeSidebarTabManager(dependencies.app);
      if (
        tabs.status !== "ready" ||
        probeApiEventTarget(dependencies.api).status !== "ready"
      ) {
        throw new Error(
          "supported sidebar seam is absent; native nodes remain available",
        );
      }
      const manager = tabs.value;
      // IMPORTANT (M23-28 named fallback): registration needs only the documented
      // `registerSidebarTab`. `getSidebarTabs` and `unregisterSidebarTab` are
      // observed-but-undocumented; when enumeration is absent, registration
      // proceeds on the extension-owned token and duplicate detection is the
      // idempotence guard above, never a probe of host internals.
      if (
        manager.enumerate !== undefined &&
        manager.enumerate().some((tab) => tab.id === tabId)
      ) {
        throw new Error(
          "H3 sidebar ID is already registered; use the existing qualified tab",
        );
      }
      const setup: SetupTransaction = {
        generation: generation + 1,
        registration: next,
        view: { mounted: false, mountedOnce: false },
        removals: [],
        destroyed: false,
      };
      generation = setup.generation;
      const graphRefresh = createGraphRefreshCoalescer(
        () => refreshGraph(true),
        dependencies.scheduleGraphRefresh,
      );
      setup.removals.push(() => graphRefresh.destroy());
      activeSetup = setup;
      const ensureSetupActive = (): void => {
        if (
          activeSetup !== setup ||
          setup.destroyed ||
          generation !== setup.generation ||
          !tabRegistered
        )
          throw new Error("sidebar registration was destroyed during setup");
      };
      const executed: EventListener = (event) => {
        const detail = record((event as CustomEvent<unknown>).detail);
        if (detail === undefined) return;
        const artifact = saveVideoArtifactFromExecuted(detail);
        if (artifact !== undefined) {
          // IMPORTANT: the untrusted locator remains transient. Only the exact
          // bounded SaveVideo result reaches the active coordinator consumer.
          try {
            next.onSaveVideoArtifact?.(artifact);
          } catch {
            // A consumer failure must not escape into ComfyUI's event dispatch.
          }
          return;
        }
        try {
          const decoded = performanceRecorder.measure("decode", () =>
            projectionFromOutput(detail.output),
          );
          const {
            projection,
            workspace,
            transactionTransparency,
            generationSequence,
            semanticProposalReview,
          } = decoded;
          recordHostProjectionTrace("decoded", {
            graph_status: latestGraph.status,
            graph_reason: latestGraph.reason,
          });
          // CRITICAL: `node` is the unique/composed execution identity; display_node is only UI ownership.
          const eventNode = detail.node;
          if (String(eventNode) !== projection.correlation.execution_node_id) {
            recordHostProjectionTrace("reject_event_node", {});
            return;
          }
          const eventPrompt = detail.prompt_id;
          if (
            eventPrompt !== undefined &&
            String(eventPrompt) !== projection.correlation.prompt_id
          ) {
            recordHostProjectionTrace("reject_event_prompt", {});
            return;
          }
          // IMPORTANT: execution can remove the weight-backed native node before
          // the ProductShell event is decoded. Always rescan on this event so the
          // entry seam can distinguish that host-owned transition from a user edit.
          refreshGraph(false, "executed");
          recordHostProjectionTrace("rescanned", {
            graph_status: latestGraph.status,
            graph_reason: latestGraph.reason,
            anchor_matches:
              latestGraph.anchors[0]?.executionId ===
              projection.correlation.execution_node_id,
            graph_failure: latestGraphFailure,
          });
          if (
            !graphMatchesProjection(
              latestGraph,
              projection.correlation.execution_node_id,
            )
          ) {
            recordHostProjectionTrace("reject_graph", {});
            return;
          }
          recordHostProjectionTrace("forward", {});
          performanceRecorder.recordProjectionBytes(detail.output);
          next.onProjection(
            projection,
            workspace,
            {
              inspection: latestGraph,
              source: "executed",
            },
            transactionTransparency,
            generationSequence,
            semanticProposalReview,
          );
        } catch {
          // An unrelated or invalid node output never becomes product state.
        }
      };
      const executionTerminal =
        (kind: ExecutionTerminalKind): EventListener =>
        (event) => {
          const detail = record((event as CustomEvent<unknown>).detail);
          const promptId = boundedPromptId(detail?.prompt_id);
          if (promptId === undefined) return;
          // CRITICAL: host terminal payloads may contain private prompts,
          // tracebacks and node outputs. Forward only the bounded correlation ID
          // and a terminal kind derived from the event channel itself.
          try {
            next.onExecutionTerminal?.({ promptId, kind });
          } catch {
            // A consumer failure must not escape into ComfyUI's event dispatch.
          }
        };
      const availability = createHostAvailabilityTracker();
      const hostAvailability =
        (eventName: string): EventListener =>
        (event) => {
          const transition = availability.observe(
            eventName,
            (event as CustomEvent<unknown>).detail,
          );
          if (transition === undefined) return;
          try {
            next.onHostAvailability?.(transition);
          } catch {
            // A consumer failure must not escape into ComfyUI's event dispatch.
          }
        };
      let firstFailure: unknown;
      let failed = false;
      const retain = (error: unknown): void => {
        if (!failed) {
          failed = true;
          firstFailure = error;
        }
      };
      try {
        // Mark before the host call so partial side effects are rolled back if it throws.
        tabRegistered = true;
        manager.register({
          id: tabId,
          get title() {
            return next.launcherCopy().title;
          },
          get tooltip() {
            return next.launcherCopy().tooltip;
          },
          icon: "pi pi-sparkles",
          type: "custom",
          render: (container) => {
            if (registration !== next && activeSetup?.registration !== next)
              throw new Error("sidebar extension is no longer active");
            mountRegistrationView(next, setup.view, container);
          },
          // IMPORTANT: native sidebar close owns only the page view. The
          // launcher, host listeners, and extension transaction remain alive.
          destroy: () => unmountRegistrationView(next, setup.view),
        });
        ensureSetupActive();
        listen(dependencies.api, "executed", executed, setup.removals);
        ensureSetupActive();
        for (const [eventName, kind] of [
          ["execution_error", "error"],
          ["execution_interrupted", "interrupted"],
          ["execution_success", "success"],
        ] as const) {
          listen(
            dependencies.api,
            eventName,
            executionTerminal(kind),
            setup.removals,
          );
          ensureSetupActive();
        }
        // The host owns the socket; these three events are the only evidence the extension has
        // that it closed. Absence of the events is a capability, not an error: `listen` returns
        // without subscribing and the shell simply never reports an interruption.
        for (const eventName of HOST_AVAILABILITY_EVENT_NAMES) {
          listen(
            dependencies.api,
            eventName,
            hostAvailability(eventName),
            setup.removals,
          );
          ensureSetupActive();
        }
        const graphEvents = probeGraphEvents(dependencies.app);
        if (graphEvents.status === "ready") {
          const rescan: EventListener = () => {
            performanceRecorder.recordGraphEvent();
            graphRefresh.request();
          };
          for (const eventName of ["change", "graphChanged", "configured"]) {
            listen(graphEvents.value, eventName, rescan, setup.removals);
            ensureSetupActive();
          }
        }
        registration = next;
        refreshGraph(false);
        ensureSetupActive();
        removals.push(...setup.removals.splice(0));
        activeSetup = undefined;
      } catch (error) {
        retain(error);
        if (activeSetup === setup) activeSetup = undefined;
        if (registration === next) registration = undefined;
        while (setup.removals.length > 0) {
          const removal = setup.removals.pop();
          if (removal === undefined) continue;
          try {
            removal();
          } catch (cleanupError) {
            retain(cleanupError);
          }
        }
        if (!setup.destroyed) {
          setup.destroyed = true;
          try {
            disposeRegistration(next);
          } catch (cleanupError) {
            retain(cleanupError);
          }
        }
        if (tabRegistered) {
          let shouldUnregister = true;
          if (manager.enumerate !== undefined)
            try {
              shouldUnregister = manager
                .enumerate()
                .some((tab) => tab.id === tabId);
            } catch (cleanupError) {
              retain(cleanupError);
            }
          tabRegistered = false;
          if (shouldUnregister) {
            if (manager.unregister === undefined) {
              retain(new Error(SIDEBAR_UNREGISTER_SEAM_ABSENT));
              tabRegistered = true;
            } else
              try {
                manager.unregister(tabId);
              } catch (cleanupError) {
                retain(cleanupError);
                tabRegistered = true;
              }
          }
        }
        throw firstFailure;
      }
    },
    refreshGraph: () => refreshGraph(false),
    performanceReceipt: () => performanceRecorder.receipt(),
    disposeExtension,
  };
}
