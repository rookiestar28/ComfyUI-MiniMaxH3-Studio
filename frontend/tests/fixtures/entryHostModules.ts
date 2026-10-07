import { canonicalStringFingerprint } from "../../src/contracts/canonicalFingerprint";
import {
  canonicalDurationResolutions,
  durationResolutionContract,
} from "../../src/contracts/generatedDurationResolution";
import { bindFrontendHostSeamFixture } from "../support/hostSeamFixture";
import { validSidebarWorkspace } from "../sidebarWorkspaceFixture";
import { retainedResponse, retainedWire } from "../retainedAssetFixtures";

type RegisteredExtension = {
  setup?: () => void;
  h3DisposeExtension?: () => void;
  beforeConfigureGraph?: () => void;
};

export type RegisteredSidebarTab = {
  id: string;
  render(container: HTMLElement): void;
  destroy(): void;
};

const canonicalGraphTypes = [
  "comfyui_h3_context.H3Context.Request",
  "comfyui_h3_context.H3Context.Plan",
  "comfyui_h3_context.H3Context.Compiler",
  "comfyui_h3_context.H3Context.Validator",
  "comfyui_h3_context.H3Context.NativeH3Adapter",
  "comfyui_h3_context.H3Context.Preview",
  "MiniMaxH3ImageToVideo",
] as const;

const graphEvents = new EventTarget();
const apiEvents = new EventTarget();
const tabs: RegisteredSidebarTab[] = [];
let extension: RegisteredExtension | undefined;
let serializedGraph: Record<string, unknown> = { nodes: [] };
let queueCount = 0;
let loadCount = 0;
const primaryWorkflow = { path: "workflows/entry-fixture.json" };
const workflowStore = {
  activeWorkflow: primaryWorkflow as object,
  openWorkflows: [primaryWorkflow] as object[],
};
let scriptedQueuePromptIds: string[] = [];
let nextQueueFailure: unknown;
const queuedPromptPayloads: unknown[] = [];
const hostLifecycleEvents: string[] = [];
export type QueuePromptWrapperMode =
  "none" | "mutate_in_place" | "forward_copy";
let queuePromptWrapperMode: QueuePromptWrapperMode = "none";
type FetchApiResponse =
  | Response
  | {
      ok: boolean;
      status: number;
      json(): Promise<unknown>;
    };
let fetchApiHandler:
  ((path: string, init: RequestInit) => Promise<FetchApiResponse>) | undefined;
const fetchApiCalls: Array<Readonly<{ path: string; init: RequestInit }>> = [];
let graphToPromptHandler: (() => Promise<unknown> | unknown) | undefined;

export function canonicalGraph(shellId = 17): Record<string, unknown> {
  return {
    nodes: [
      ...canonicalGraphTypes.map((type, index) => ({ id: index + 10, type })),
      {
        id: shellId,
        type: "comfyui_h3_context.H3Context.ProductShell",
      },
    ],
  };
}

function executedOutput(
  executionNodeId: string,
  promptId: string,
  generationSequence?: Record<string, unknown>,
  semanticProposalReview?: Record<string, unknown>,
  workspaceId?: string,
  requestedSeconds?: number,
  taskMode?: "t2va" | "i2va" | "fl2va" | "l2va" | "ref2va",
  effectiveDurationMilliseconds?: number,
  effectiveFrameCount?: number,
): Record<string, unknown[]> {
  const fingerprint = canonicalStringFingerprint(
    validSidebarWorkspace.prompt_text,
  );
  const generationReady = generationSequence !== undefined;
  const projection = {
    schema: "h3.context.product.shell.v1",
    product_scope: "MANUAL_ONLY_SCOPED",
    qualification_plan_fingerprint: fingerprint,
    report_id: "report-1",
    report_revision: 1,
    report_fingerprint: fingerprint,
    prompt_fingerprint: fingerprint,
    correlation: {
      prompt_id: promptId,
      execution_node_id: executionNodeId,
    },
    task_mode: taskMode ?? (generationReady ? "t2va" : "ref2va"),
    profile: "h3_full_reference",
    host: {
      node_api: "V1_ONLY",
      core_version: "0.32.0",
      core_revision: "b323a345bbbfb2f3a95b5b73b68eb7919a26515e", // pragma: allowlist secret
      frontend_version: "1.48.7",
      frontend_revision: "6d6af63c00f132cd25dc29307fc56bd2c094fa22", // pragma: allowlist secret
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
      workspace_id: workspaceId ?? validSidebarWorkspace.workspace_id,
      report_fingerprint: fingerprint,
      prompt_fingerprint: fingerprint,
      base_prompt_fingerprint: fingerprint,
      correlation: projection.correlation,
      task_mode: projection.task_mode,
      bindings: [],
      reference_candidates: [],
      ...(requestedSeconds === undefined
        ? {}
        : {
            // Requested and effective duration deliberately diverge for snapped
            // fixture rows; mirroring one into both fields hid M23-40.
            capabilities: {
              ...validSidebarWorkspace.capabilities,
              output_duration: {
                ...validSidebarWorkspace.capabilities.output_duration,
                requested_seconds: requestedSeconds,
                effective_seconds:
                  (effectiveDurationMilliseconds ?? requestedSeconds * 1000) /
                  1000,
              },
            },
            planning: {
              ...validSidebarWorkspace.planning,
              timeline: {
                ...validSidebarWorkspace.planning.timeline,
                end_seconds:
                  (effectiveDurationMilliseconds ?? requestedSeconds * 1000) /
                  1000,
                effective_frame_count:
                  effectiveFrameCount ?? requestedSeconds * 24,
                effective_duration_milliseconds:
                  effectiveDurationMilliseconds ?? requestedSeconds * 1000,
              },
            },
          }),
      proposal: {
        ...validSidebarWorkspace.proposal,
        base_prompt_fingerprint: fingerprint,
        current_prompt_fingerprint: fingerprint,
      },
    },
  ];
  if (generationSequence !== undefined)
    output.generation_sequence = [
      {
        ...generationSequence,
        correlation: {
          prompt_id: promptId,
          execution_node_id: executionNodeId,
        },
      },
    ];
  if (semanticProposalReview !== undefined)
    output.semantic_proposal_review = [
      {
        ...semanticProposalReview,
        report_fingerprint: projection.report_fingerprint,
        correlation: projection.correlation,
      },
    ];
  return output;
}

const graphSerialize = (): Record<string, unknown> =>
  structuredClone(serializedGraph);
const graphChange = (): void => {};
const graphSetDirtyCanvas = (
  _foreground: boolean,
  _background?: boolean,
): void => {};
const graphGetNodeById = (id: string | number) => {
  const nodes = Array.isArray(serializedGraph.nodes)
    ? (serializedGraph.nodes as Record<string, unknown>[])
    : [];
  const node = nodes.find((candidate) => String(candidate.id) === String(id));
  if (node === undefined) return undefined;
  const values = Array.isArray(node.widgets_values) ? node.widgets_values : [];
  const names =
    node.type === "comfyui_h3_context.H3Context.Request"
      ? ["task_mode", "user_intent", "duration_seconds"]
      : node.type === "PrimitiveFloat"
        ? ["value"]
        : [];
  return {
    type: node.type,
    widgets: names.map((name, index) => ({
      name,
      get value(): unknown {
        return values[index];
      },
      set value(value: unknown) {
        values[index] = value;
        const named = node.widgets_values_named;
        if (
          named !== null &&
          typeof named === "object" &&
          !Array.isArray(named)
        )
          (named as Record<string, unknown>)[name] = value;
      },
      callback: (_value: unknown): void => {},
    })),
    onWidgetChanged: (
      _name: string,
      _value: unknown,
      _previous: unknown,
      _widget: unknown,
    ): void => {},
  };
};
class EntryFixtureDetachedGraph {
  readonly serialized: unknown;

  constructor(serialized?: unknown) {
    this.serialized = structuredClone(serialized);
  }
}
const graphHost = {
  // IMPORTANT: App Mode compiles a candidate through the live graph class before
  // the sole canvas write; a plain object here would model an incompatible host.
  constructor: EntryFixtureDetachedGraph,
  serialize: graphSerialize,
  events: graphEvents,
  getNodeById: graphGetNodeById,
  change: graphChange,
  setDirtyCanvas: graphSetDirtyCanvas,
};
const loadApiJson = (): void => {
  loadCount += 1;
};
const graphToPrompt = async () =>
  graphToPromptHandler?.() ?? { output: {}, workflow: {} };
const loadGraphData = (
  value: unknown,
  _clean = true,
  _restoreView = true,
  workflow: object | null = null,
): void => {
  serializedGraph = structuredClone(value as Record<string, unknown>);
  loadCount += 1;
  if (workflow === null) {
    const temporary = { path: `workflows/entry-copy-${loadCount}.json` };
    workflowStore.openWorkflows.push(temporary);
    workflowStore.activeWorkflow = temporary;
    return;
  }
  workflowStore.activeWorkflow = workflow;
  if (!workflowStore.openWorkflows.includes(workflow))
    workflowStore.openWorkflows.push(workflow);
};
const registerSidebarTab = (tab: RegisteredSidebarTab): void => {
  tabs.push(tab);
};
const unregisterSidebarTab = (id: string): void => {
  const index = tabs.findIndex((tab) => tab.id === id);
  if (index >= 0) tabs.splice(index, 1);
};
const getSidebarTabs = (): RegisteredSidebarTab[] => tabs;
const registerExtension = (value: RegisteredExtension): void => {
  extension = value;
};
const queuePrompt = async (
  _number?: number,
  prompt?: unknown,
): Promise<{
  prompt_id: string;
  number: number;
  node_errors: Record<string, unknown>;
}> => {
  let forwardedPrompt = prompt;
  if (queuePromptWrapperMode !== "none") {
    if (prompt === null || typeof prompt !== "object" || Array.isArray(prompt))
      throw new TypeError("queue wrapper received no prompt envelope");
    const envelope = prompt as Record<string, unknown>;
    const workflowValue = envelope.workflow;
    if (
      workflowValue === null ||
      typeof workflowValue !== "object" ||
      Array.isArray(workflowValue)
    )
      throw new TypeError("queue wrapper received no workflow envelope");
    const workflow = workflowValue as Record<string, unknown>;
    // CRITICAL: model both installed wrapper families. One mutates the caller's
    // workflow; another compatible wrapper may forward a new envelope instead.
    if (queuePromptWrapperMode === "mutate_in_place") {
      workflow.widget_idx_map = {};
      workflow.seed_widgets = {};
    } else {
      forwardedPrompt = {
        ...envelope,
        workflow: {
          ...workflow,
          widget_idx_map: {},
          seed_widgets: {},
        },
      };
    }
  }
  queueCount += 1;
  queuedPromptPayloads.push(forwardedPrompt);
  if (nextQueueFailure !== undefined) {
    const error = nextQueueFailure;
    nextQueueFailure = undefined;
    throw error;
  }
  const promptId = scriptedQueuePromptIds.shift() ?? "prompt-1";
  hostLifecycleEvents.push(`queue:${promptId}`);
  return { prompt_id: promptId, number: -queueCount, node_errors: {} };
};
const fetchApi = async (path: string, init: RequestInit) => {
  fetchApiCalls.push(Object.freeze({ path, init }));
  if (
    path === "/h3-context/v1/production/action" ||
    path === "/h3-context/v1/generation/coordinator"
  ) {
    try {
      const body = JSON.parse(String(init.body)) as { action?: unknown };
      if (typeof body.action === "string")
        hostLifecycleEvents.push(
          `${path.includes("production") ? "production" : "coordinator"}:${body.action}`,
        );
    } catch {
      hostLifecycleEvents.push("invalid-action-request");
    }
  }
  if (path === "/h3-context/v1/duration/resolve") {
    const request = JSON.parse(String(init.body)) as {
      schema?: unknown;
      requested_seconds?: unknown;
    };
    const fixture = canonicalDurationResolutions.find(
      (candidate) =>
        request.schema === durationResolutionContract.request_schema &&
        candidate.requested_seconds === request.requested_seconds &&
        Object.keys(request).sort().join("\u0000") ===
          "requested_seconds\u0000schema",
    );
    if (fixture !== undefined) {
      return {
        ok: true,
        status: 200,
        json: async () => fixture,
        text: async () => JSON.stringify(fixture),
      };
    }
    return {
      ok: false,
      status: 422,
      json: async () => ({}),
      text: async () => "{}",
    };
  }
  if (path === "/h3-context/retained-assets") {
    const request = JSON.parse(String(init.body)) as Record<string, unknown>;
    if (
      request.intent === "status" &&
      Object.keys(request).sort().join("\u0000") === "intent"
    ) {
      const wire = retainedWire(false);
      return retainedResponse({
        ...wire,
        projection: {
          ...wire.projection,
          revision: 0,
          count: 0,
          charged_bytes: 0,
          assets: [],
        },
      });
    }
  }
  if (path === "/h3-context/project-recovery") {
    const request = JSON.parse(String(init.body)) as Record<string, unknown>;
    if (request.intent === "status" && request.project_id === null) {
      return retainedResponse({
        schema: "h3.context.project_recovery.status.v1",
        supported: true,
        enabled: false,
        include_video: false,
        revision: 0,
        charged_bytes: 0,
        records: [],
        current: null,
        writer_active: false,
      });
    }
  }
  return (
    fetchApiHandler?.(path, init) ??
    Promise.resolve({
      ok: false,
      status: 503,
      json: async () => ({}),
    })
  );
};
const fileURL = (route: string): string => route;
const uiSettings = Object.assign(new EventTarget(), {
  getSettingValue: (_id: string): unknown => undefined,
  setSettingValue: (_id: string, _value: unknown): void => {},
  setSettingValueAsync: async (
    _id: string,
    _value: unknown,
  ): Promise<void> => {},
});
const registeredNodeTypes = {
  SyntheticH3Node: { nodeData: { name: "SyntheticH3Node" } },
};

export const hostSeamFixture = bindFrontendHostSeamFixture({
  "frontend.app.canvas.keyboard_capture": (notify: () => void) =>
    acquireCanvasKeyboardGuard(app, notify),
  "frontend.app.modal_keyboard_guard": () => acquireModalKeyboardGuard(app),
  "frontend.api.event_target": apiEvents,
  "frontend.api.fetch_api": fetchApi,
  "frontend.api.file_url": fileURL,
  "frontend.api.queue_prompt": queuePrompt,
  "frontend.app.extension_manager.get_sidebar_tabs": getSidebarTabs,
  "frontend.app.extension_manager.register_sidebar_tab": registerSidebarTab,
  "frontend.app.extension_manager.unregister_sidebar_tab": unregisterSidebarTab,
  "frontend.app.extension_manager.workflow.active_workflow":
    workflowStore.activeWorkflow,
  "frontend.app.extension_manager.workflow.open_workflows":
    workflowStore.openWorkflows,
  "frontend.app.graph": graphHost,
  "frontend.app.graph.change": graphChange,
  "frontend.app.graph.events": graphEvents,
  "frontend.app.graph.get_node_by_id": graphGetNodeById,
  "frontend.app.graph.serialize": graphSerialize,
  "frontend.app.graph.set_dirty_canvas": graphSetDirtyCanvas,
  "frontend.app.graph_to_prompt": graphToPrompt,
  "frontend.app.load_api_json": loadApiJson,
  "frontend.app.load_graph_data": loadGraphData,
  "frontend.app.register_extension": registerExtension,
  "frontend.app.ui_settings": uiSettings,
  "frontend.litegraph.registered_node_types": registeredNodeTypes,
});

// D13 host behaviour (observed on frontend 1.51.9): `app.graph` returns `undefined` until the
// host has created its root graph, and extension modules are evaluated before that point.
// M23-36 models it here so module-evaluation-time reads are visible to the hermetic lane.
let graphInitialized = true;
export function setGraphInitialized(initialized: boolean): void {
  graphInitialized = initialized;
}

const FixtureAppConstructor = function FixtureAppConstructor() {};
Object.defineProperty(FixtureAppConstructor, "maskeditor_is_opended", {
  value: null,
  configurable: true,
  writable: true,
});
const fixtureCanvas = {
  canvas: document.createElement("canvas"),
  _events_binded: true,
  _key_callback: (() => undefined) as EventListener,
  _ghostKeyHandler: null as EventListener | null,
  state: { ghostNodeId: null as string | null },
};

export const app = {
  constructor: FixtureAppConstructor,
  canvas: fixtureCanvas,
  get graph(): typeof graphHost {
    return (graphInitialized
      ? graphHost
      : undefined) as unknown as typeof graphHost;
  },
  loadApiJson,
  graphToPrompt,
  loadGraphData,
  extensionManager: {
    registerSidebarTab,
    unregisterSidebarTab,
    getSidebarTabs,
    workflow: workflowStore,
  },
  registerExtension,
  ui: { settings: uiSettings },
};

export const api = {
  addEventListener: apiEvents.addEventListener.bind(apiEvents),
  removeEventListener: apiEvents.removeEventListener.bind(apiEvents),
  queuePrompt,
  fetchApi,
  fileURL,
};

export function setFetchApiHandler(
  handler:
    | ((path: string, init: RequestInit) => Promise<FetchApiResponse>)
    | undefined,
): void {
  fetchApiHandler = handler;
}

export function setGraphToPromptHandler(
  handler: (() => Promise<unknown> | unknown) | undefined,
): void {
  graphToPromptHandler = handler;
}

export function productionFetchBodies(): Array<Record<string, unknown>> {
  return fetchApiCalls
    .filter(({ path }) => path === "/h3-context/v1/production/action")
    .map(
      ({ init }) => JSON.parse(String(init.body)) as Record<string, unknown>,
    );
}

export function durationResolutionRequests(): readonly number[] {
  return fetchApiCalls
    .filter(({ path }) => path === durationResolutionContract.route)
    .map(({ init }) => {
      const request = JSON.parse(String(init.body)) as {
        requested_seconds?: unknown;
      };
      return request.requested_seconds;
    })
    .filter((value): value is number => typeof value === "number");
}

export function setGraph(value: Record<string, unknown>): void {
  serializedGraph = structuredClone(value);
}

export function setQueuePromptFailure(error: unknown): void {
  nextQueueFailure = error;
}

export function setQueuePromptIds(promptIds: readonly string[]): void {
  scriptedQueuePromptIds = [...promptIds];
}

export function setQueuePromptWrapperMode(mode: QueuePromptWrapperMode): void {
  queuePromptWrapperMode = mode;
}

export function queuedPrompts(): readonly unknown[] {
  return [...queuedPromptPayloads];
}

export function lifecycleEvents(): readonly string[] {
  return [...hostLifecycleEvents];
}

export function dispatchProjection(
  executionNodeId = "17",
  options: Readonly<{
    promptId?: string;
    generationSequence?: Record<string, unknown>;
    semanticProposalReview?: Record<string, unknown>;
    workspaceId?: string;
    requestedSeconds?: number;
    taskMode?: "t2va" | "i2va" | "fl2va" | "l2va" | "ref2va";
    effectiveDurationMilliseconds?: number;
    effectiveFrameCount?: number;
  }> = {},
): void {
  const promptId = options.promptId ?? "prompt-1";
  apiEvents.dispatchEvent(
    new CustomEvent("executed", {
      detail: {
        node: executionNodeId,
        prompt_id: promptId,
        output: executedOutput(
          executionNodeId,
          promptId,
          options.generationSequence,
          options.semanticProposalReview,
          options.workspaceId,
          options.requestedSeconds,
          options.taskMode,
          options.effectiveDurationMilliseconds,
          options.effectiveFrameCount,
        ),
      },
    }),
  );
}

export function dispatchExecutionTerminal(
  kind: "error" | "interrupted" | "success",
  promptId: string,
  extra: Record<string, unknown> = {},
): void {
  const eventName =
    kind === "error"
      ? "execution_error"
      : kind === "interrupted"
        ? "execution_interrupted"
        : "execution_success";
  apiEvents.dispatchEvent(
    new CustomEvent(eventName, {
      detail: { ...extra, prompt_id: promptId },
    }),
  );
}

/**
 * The three host socket events. `status` with a null payload is exactly how the frontend reports
 * a closed socket, so the fixture keeps the payload rather than inventing a dedicated event.
 */
export function dispatchHostStatus(detail: unknown): void {
  apiEvents.dispatchEvent(new CustomEvent("status", { detail }));
}

export function dispatchHostSocketDropped(): void {
  apiEvents.dispatchEvent(new CustomEvent("status", { detail: null }));
}

export function dispatchHostReconnecting(): void {
  apiEvents.dispatchEvent(new CustomEvent("reconnecting", { detail: null }));
}

export function dispatchHostReconnected(): void {
  apiEvents.dispatchEvent(new CustomEvent("reconnected", { detail: null }));
}

export function dispatchSaveVideoArtifact(
  promptId: string,
  outputNodeId = "43",
): void {
  apiEvents.dispatchEvent(
    new CustomEvent("executed", {
      detail: {
        node: outputNodeId,
        prompt_id: promptId,
        output: {
          images: [
            {
              filename: "managed_00001_.mp4",
              subfolder: "video/h3-context",
              type: "output",
            },
          ],
          animated: [true],
        },
      },
    }),
  );
}

export function registeredExtension(): RegisteredExtension {
  if (extension === undefined)
    throw new Error("entry extension was not registered");
  return extension;
}

export function registeredTab(): RegisteredSidebarTab {
  const tab = tabs.find((candidate) => candidate.id === "h3-context");
  if (tab === undefined)
    throw new Error("entry sidebar tab was not registered");
  return tab;
}

export function sideEffectCounts(): { loads: number; queues: number } {
  return { loads: loadCount, queues: queueCount };
}

export function workflowState(): {
  activeWorkflow: object;
  primaryWorkflow: object;
  openWorkflowCount: number;
} {
  return {
    activeWorkflow: workflowStore.activeWorkflow,
    primaryWorkflow,
    openWorkflowCount: workflowStore.openWorkflows.length,
  };
}
import { acquireModalKeyboardGuard } from "../../src/host/modalKeyboardGuard";
import { acquireCanvasKeyboardGuard } from "../../src/host/canvasKeyboardGuard";
