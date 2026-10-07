import type { AppModeApi, AppModeApp } from "../../src/host/appMode";
import {
  createHostWorkflowBehaviourFixture,
  type HostWorkflowBehaviourFixture,
  type HostWorkflowBehaviourVariantId,
} from "./hostBehaviourCatalogue";
import { bindFrontendHostSeamFixture } from "./hostSeamFixture";
import { acquireModalKeyboardGuard } from "../../src/host/modalKeyboardGuard";
import { acquireCanvasKeyboardGuard } from "../../src/host/canvasKeyboardGuard";

const noop = (): undefined => undefined;
const asyncNoop = async (): Promise<undefined> => undefined;
const defaultGraph = {
  serialize: noop,
  events: new EventTarget(),
  getNodeById: noop,
  change: noop,
  setDirtyCanvas: noop,
};
const defaultApiEvents = new EventTarget();
const defaultActiveWorkflow = Object.freeze({
  path: "workflows/fixture-active.json",
});
const defaultOpenWorkflows = Object.freeze([defaultActiveWorkflow]);
const defaultSettings = Object.assign(new EventTarget(), {
  getSettingValue: noop,
  setSettingValue: noop,
});
const FixtureDetachedGraph = function (serialized?: unknown): object {
  return (serialized ?? {}) as object;
} as unknown as new (serialized?: unknown) => unknown;

const defaultCanvasKey: EventListener = () => undefined;

function cloneFixtureApp<App extends object>(
  source: App,
  overrides: object,
): App {
  const descriptors = Object.getOwnPropertyDescriptors(source);
  const values: Record<PropertyKey, unknown> = {};
  // IMPORTANT: object spread invokes capability getters and drops non-enumerable fields.
  // Preserve constructor/canvas descriptors and prototype through every fixture clone.
  for (const name of Reflect.ownKeys(descriptors)) {
    if (
      name === "constructor" ||
      name === "canvas" ||
      !descriptors[name as string]?.enumerable
    )
      continue;
    Object.defineProperty(values, name, {
      value: Reflect.get(source, name),
      enumerable: true,
      configurable: true,
      writable: true,
    });
  }
  const cloned = Object.create(Object.getPrototypeOf(source)) as App;
  Object.defineProperties(
    cloned,
    Object.getOwnPropertyDescriptors({ ...values, ...overrides }),
  );
  for (const name of ["constructor", "canvas"])
    if (Object.hasOwn(descriptors, name))
      Object.defineProperty(cloned, name, descriptors[name]);
  return cloned;
}

function withKeyboardCapabilities<App extends object>(source: App): App {
  const cloned = cloneFixtureApp(source, {});
  if (Object.getPrototypeOf(source) !== Object.prototype) return cloned;
  if (Object.getOwnPropertyDescriptor(source, "constructor") === undefined) {
    const constructor = function FixtureAppConstructor() {};
    Object.defineProperty(constructor, "maskeditor_is_opended", {
      value: null,
      configurable: true,
      writable: true,
    });
    Object.defineProperty(cloned, "constructor", {
      value: constructor,
      configurable: true,
      writable: true,
    });
  }
  if (
    Object.getOwnPropertyDescriptor(source, "canvas") === undefined &&
    typeof document !== "undefined"
  ) {
    document.addEventListener("keyup", defaultCanvasKey, true);
    Object.defineProperty(cloned, "canvas", {
      configurable: true,
      writable: true,
      value: {
        canvas: document.createElement("canvas"),
        _events_binded: true,
        _key_callback: defaultCanvasKey,
        _ghostKeyHandler: null,
        state: { ghostNodeId: null },
      },
    });
  }
  return cloned;
}

type NoisyJson = Record<string, any>;

/** D12 hermetic fake for callbacks installed by co-resident frontend packs. */
export function createNoisyHostExtension() {
  let revision = 0;
  return Object.freeze({
    nodeCreated(node: NoisyJson): void {
      if (node === null || typeof node !== "object" || Array.isArray(node))
        return;
      const properties =
        node.properties !== null &&
        typeof node.properties === "object" &&
        !Array.isArray(node.properties)
          ? node.properties
          : {};
      node.properties = { ...properties, cnr_id: "fixture-noisy-pack" };
    },
    afterConfigureGraph(workflow: NoisyJson): void {
      if (
        workflow === null ||
        typeof workflow !== "object" ||
        Array.isArray(workflow)
      )
        return;
      revision += 1;
      const extra =
        workflow.extra !== null &&
        typeof workflow.extra === "object" &&
        !Array.isArray(workflow.extra)
          ? workflow.extra
          : {};
      workflow.extra = {
        ...extra,
        ds: { scale: 1 + revision / 100, offset: [revision, revision] },
        h3NoisyHostFixture: { revision },
      };
      const nodes = Array.isArray(workflow.nodes) ? workflow.nodes : [];
      const movable = nodes.find(
        (node: unknown): node is NoisyJson =>
          node !== null && typeof node === "object" && !Array.isArray(node),
      );
      if (movable !== undefined) {
        const pos = Array.isArray(movable.pos) ? movable.pos : [0, 0];
        movable.pos = [Number(pos[0] ?? 0) + revision, Number(pos[1] ?? 0)];
      }
      const seed = nodes.find((node: unknown): node is NoisyJson => {
        if (node === null || typeof node !== "object" || Array.isArray(node))
          return false;
        const candidate = node as NoisyJson;
        return (
          /seed|sampler/i.test(String(candidate.type ?? "")) &&
          Array.isArray(candidate.widgets_values) &&
          typeof candidate.widgets_values[0] === "number"
        );
      });
      if (seed === undefined) {
        workflow.widget_idx_map = {};
        workflow.seed_widgets = {};
        return;
      }
      const seedId = String(seed.id);
      workflow.widget_idx_map = { [seedId]: { seed: 0 } };
      workflow.seed_widgets = { [seedId]: 0 };
      seed.widgets_values = [
        seed.widgets_values[0] + revision,
        ...seed.widgets_values.slice(1),
      ];
    },
  });
}

function validateFixtureBackedValues(app: AppModeApp, api: AppModeApi): void {
  const graph = app.graph ?? defaultGraph;
  const graphRecord = graph as AppModeApp["graph"] & {
    events?: unknown;
  };
  bindFrontendHostSeamFixture({
    "frontend.app.canvas.keyboard_capture": (notify: () => void) =>
      acquireCanvasKeyboardGuard(app, notify),
    "frontend.app.modal_keyboard_guard": () => acquireModalKeyboardGuard(app),
    "frontend.api.event_target": defaultApiEvents,
    "frontend.api.fetch_api": api.fetchApi ?? asyncNoop,
    "frontend.api.file_url": api.fileURL ?? ((route: string) => route),
    "frontend.api.queue_prompt": api.queuePrompt ?? asyncNoop,
    "frontend.app.extension_manager.get_sidebar_tabs": noop,
    "frontend.app.extension_manager.register_sidebar_tab": noop,
    "frontend.app.extension_manager.unregister_sidebar_tab": noop,
    "frontend.app.extension_manager.workflow.active_workflow":
      app.extensionManager?.workflow?.activeWorkflow ?? defaultActiveWorkflow,
    "frontend.app.extension_manager.workflow.open_workflows": Array.isArray(
      app.extensionManager?.workflow?.openWorkflows,
    )
      ? app.extensionManager.workflow.openWorkflows
      : defaultOpenWorkflows,
    "frontend.app.graph": graph,
    "frontend.app.graph.change": graph.change ?? noop,
    "frontend.app.graph.events": graphRecord.events ?? defaultGraph.events,
    "frontend.app.graph.get_node_by_id": graph.getNodeById ?? noop,
    "frontend.app.graph.serialize": graph.serialize ?? noop,
    "frontend.app.graph.set_dirty_canvas": graph.setDirtyCanvas ?? noop,
    "frontend.app.graph_to_prompt": app.graphToPrompt ?? asyncNoop,
    "frontend.app.load_api_json": app.loadApiJson ?? noop,
    "frontend.app.load_graph_data": app.loadGraphData ?? noop,
    "frontend.app.register_extension": noop,
    "frontend.app.ui_settings": defaultSettings,
    "frontend.litegraph.registered_node_types": {
      SyntheticFixtureNode: { nodeData: { name: "SyntheticFixtureNode" } },
    },
  });
}

/**
 * Create an App Mode test host through the HC-09 shape authority while retaining
 * the exact generic mock types supplied by each focused test.
 */
export function fixtureBackedAppModeHost<
  App extends AppModeApp,
  Api extends AppModeApi,
>(
  app: App,
  api: Api,
  options: Readonly<{
    workflowVariant?:
      HostWorkflowBehaviourVariantId | HostWorkflowBehaviourFixture;
  }> = {},
): Readonly<{
  app: App;
  api: Api;
  workflowBehaviour: HostWorkflowBehaviourFixture | undefined;
}> {
  const workflowBehaviour =
    options.workflowVariant === undefined
      ? undefined
      : typeof options.workflowVariant === "string"
        ? createHostWorkflowBehaviourFixture(options.workflowVariant)
        : options.workflowVariant;
  const keyboardBoundApp = withKeyboardCapabilities(app);
  const variantBoundApp = (
    workflowBehaviour === undefined
      ? keyboardBoundApp
      : cloneFixtureApp(keyboardBoundApp, {
          extensionManager: {
            ...keyboardBoundApp.extensionManager,
            workflow: workflowBehaviour.workflowStore,
          },
        })
  ) as App;
  const activeWorkflow =
    variantBoundApp.extensionManager?.workflow?.activeWorkflow;
  const workflowBoundApp = (
    workflowBehaviour !== undefined ||
    (activeWorkflow !== null &&
      typeof activeWorkflow === "object" &&
      !Array.isArray(activeWorkflow))
      ? variantBoundApp
      : cloneFixtureApp(variantBoundApp, {
          extensionManager: {
            ...variantBoundApp.extensionManager,
            workflow: {
              ...variantBoundApp.extensionManager?.workflow,
              activeWorkflow: Object.freeze({
                path: "workflows/fixture-active.json",
              }),
            },
          },
        })
  ) as App;
  const originalGraph = workflowBoundApp.graph;
  const liveConstructor = (
    originalGraph as unknown as { constructor?: unknown } | undefined
  )?.constructor;
  // IMPORTANT: the detached compiler is a separate public host seam. Plain
  // object fixtures inherit Object, which is not an LGraph constructor, so the
  // hermetic host declares an explicit constructor that returns the candidate snapshot.
  const constructorBoundApp = (
    originalGraph === undefined
      ? workflowBoundApp
      : cloneFixtureApp(workflowBoundApp, {
          graph: {
            ...originalGraph,
            constructor:
              typeof liveConstructor === "function" &&
              liveConstructor !== Object
                ? liveConstructor
                : FixtureDetachedGraph,
          },
        })
  ) as App;
  const originalSerialize = constructorBoundApp.graph?.serialize;
  const originalLoad = constructorBoundApp.loadGraphData;
  let loadedGraph: unknown;
  const snapshot = (value: unknown): string | undefined => {
    try {
      return JSON.stringify(value);
    } catch {
      return undefined;
    }
  };
  // IMPORTANT: the hermetic host must model the public load/serialize pair as
  // one seam. A bare vi.fn() load that leaves serialize on the old graph makes
  // a valid post-write owned-projection check indistinguishable from host drift.
  const fixtureApp = (
    typeof originalLoad === "function" &&
    typeof originalSerialize === "function"
      ? cloneFixtureApp(constructorBoundApp, {
          graph: {
            ...constructorBoundApp.graph,
            serialize: () =>
              loadedGraph === undefined ? originalSerialize() : loadedGraph,
          },
          loadGraphData: new Proxy(originalLoad, {
            apply(target, thisArg, argumentsList) {
              const before = originalSerialize();
              const beforeSnapshot = snapshot(before);
              const result = Reflect.apply(target, thisArg, argumentsList);
              const captureLoadedGraph = () => {
                const after = originalSerialize();
                const afterSnapshot = snapshot(after);
                const hostModeledWrite =
                  beforeSnapshot !== undefined && afterSnapshot !== undefined
                    ? afterSnapshot !== beforeSnapshot
                    : after !== before;
                loadedGraph = hostModeledWrite ? after : argumentsList[0];
              };
              if (result instanceof Promise)
                return result.then(
                  (value) => {
                    captureLoadedGraph();
                    return value;
                  },
                  (error) => {
                    throw error;
                  },
                );
              captureLoadedGraph();
              return result;
            },
          }),
        })
      : constructorBoundApp
  ) as App;
  // IMPORTANT: HC-09 validates the steady-state object shape, while an explicitly selected
  // lifecycle catalogue row must retain its pre-attach null state or fresh-start tests go green
  // against a workflow authority the supported host has not created yet.
  validateFixtureBackedValues(fixtureApp, api);
  return Object.freeze({ app: fixtureApp, api, workflowBehaviour });
}

export function fixtureBackedAppModeApi<const Api extends AppModeApi>(
  api: Api,
): Api {
  validateFixtureBackedValues({}, api);
  return api;
}

type SidebarHost = {
  graph?: object;
  extensionManager?: {
    getSidebarTabs?: unknown;
    registerSidebarTab?: unknown;
    unregisterSidebarTab?: unknown;
  };
};

type SidebarApi = {
  addEventListener?: unknown;
  removeEventListener?: unknown;
};

function sidebarEventTarget(api: SidebarApi): object {
  return typeof api.addEventListener === "function" &&
    typeof api.removeEventListener === "function"
    ? api
    : defaultApiEvents;
}

export function fixtureBackedSidebarHost<const Host extends SidebarHost>(
  host: Host,
): Host {
  const keyboardHost = withKeyboardCapabilities(host);
  const manager = host.extensionManager ?? {};
  const graph = (host.graph ?? defaultGraph) as {
    serialize?: unknown;
    events?: unknown;
  };
  bindFrontendHostSeamFixture({
    "frontend.app.canvas.keyboard_capture": (notify: () => void) =>
      acquireCanvasKeyboardGuard(keyboardHost, notify),
    "frontend.app.modal_keyboard_guard": () =>
      acquireModalKeyboardGuard(keyboardHost),
    "frontend.api.event_target": defaultApiEvents,
    "frontend.api.fetch_api": asyncNoop,
    "frontend.api.file_url": (route: string) => route,
    "frontend.api.queue_prompt": asyncNoop,
    "frontend.app.extension_manager.get_sidebar_tabs":
      typeof manager.getSidebarTabs === "function"
        ? manager.getSidebarTabs
        : noop,
    "frontend.app.extension_manager.register_sidebar_tab":
      typeof manager.registerSidebarTab === "function"
        ? manager.registerSidebarTab
        : noop,
    "frontend.app.extension_manager.unregister_sidebar_tab":
      typeof manager.unregisterSidebarTab === "function"
        ? manager.unregisterSidebarTab
        : noop,
    "frontend.app.extension_manager.workflow.active_workflow":
      defaultActiveWorkflow,
    "frontend.app.extension_manager.workflow.open_workflows":
      defaultOpenWorkflows,
    "frontend.app.graph": graph,
    "frontend.app.graph.change": defaultGraph.change,
    "frontend.app.graph.events": graph.events ?? defaultGraph.events,
    "frontend.app.graph.get_node_by_id": defaultGraph.getNodeById,
    "frontend.app.graph.serialize":
      typeof graph.serialize === "function" ? graph.serialize : noop,
    "frontend.app.graph.set_dirty_canvas": defaultGraph.setDirtyCanvas,
    "frontend.app.graph_to_prompt": asyncNoop,
    "frontend.app.load_api_json": noop,
    "frontend.app.load_graph_data": noop,
    "frontend.app.register_extension": noop,
    "frontend.app.ui_settings": defaultSettings,
    "frontend.litegraph.registered_node_types": {
      SyntheticFixtureNode: { nodeData: { name: "SyntheticFixtureNode" } },
    },
  });
  return host;
}

export function fixtureBackedSidebarApi<const Api extends SidebarApi>(
  api: Api,
): Api {
  const keyboardHost = withKeyboardCapabilities({});
  bindFrontendHostSeamFixture({
    "frontend.app.canvas.keyboard_capture": (notify: () => void) =>
      acquireCanvasKeyboardGuard(keyboardHost, notify),
    "frontend.app.modal_keyboard_guard": () =>
      acquireModalKeyboardGuard(keyboardHost),
    "frontend.api.event_target": sidebarEventTarget(api),
    "frontend.api.fetch_api": asyncNoop,
    "frontend.api.file_url": (route: string) => route,
    "frontend.api.queue_prompt": asyncNoop,
    "frontend.app.extension_manager.get_sidebar_tabs": noop,
    "frontend.app.extension_manager.register_sidebar_tab": noop,
    "frontend.app.extension_manager.unregister_sidebar_tab": noop,
    "frontend.app.extension_manager.workflow.active_workflow":
      defaultActiveWorkflow,
    "frontend.app.extension_manager.workflow.open_workflows":
      defaultOpenWorkflows,
    "frontend.app.graph": defaultGraph,
    "frontend.app.graph.change": defaultGraph.change,
    "frontend.app.graph.events": defaultGraph.events,
    "frontend.app.graph.get_node_by_id": defaultGraph.getNodeById,
    "frontend.app.graph.serialize": defaultGraph.serialize,
    "frontend.app.graph.set_dirty_canvas": defaultGraph.setDirtyCanvas,
    "frontend.app.graph_to_prompt": asyncNoop,
    "frontend.app.load_api_json": noop,
    "frontend.app.load_graph_data": noop,
    "frontend.app.register_extension": noop,
    "frontend.app.ui_settings": defaultSettings,
    "frontend.litegraph.registered_node_types": {
      SyntheticFixtureNode: { nodeData: { name: "SyntheticFixtureNode" } },
    },
  });
  return api;
}

export function fixtureBackedSidebarDependencies<
  const Dependencies extends Readonly<{
    app: SidebarHost;
    api: SidebarApi;
  }>,
>(dependencies: Dependencies): Dependencies {
  fixtureBackedSidebarHost(dependencies.app);
  fixtureBackedSidebarApi(dependencies.api);
  return dependencies;
}
