// Typed host-seam probes (M23-28).
//
// Every product read of a ComfyUI `app`/`api`/LiteGraph member goes through exactly one owner
// module per seam family:
//   - this module: graph read, compile, host API, UI settings and registration families;
//   - `canvasOwnedWrite.ts`: the workflow store and `loadGraphData` (the only writer);
//   - `queueSeam.ts`: the queue callable (the only queue owner).
// A probe returns a `SeamResult`; product modules branch on that result and never on host
// internals. Probes read the member at the moment they are called and cache nothing, so the
// pinned host's call order and count stay exactly where the callers put them.
//
// IMPORTANT: a probe never guesses from a version string or DOM presence. An absent or
// reshaped member is a named refusal; the only fallbacks are the documented surfaces recorded by
// the companion host-seam probe policy contract. Keep its JSON basename out of this comment: the
// packaged-artifact reader scan treats any exact basename in frontend source as a bundle input.

export type SeamRefusal =
  | "graph_unreadable"
  | "graph_unavailable"
  | "graph_serialize_unavailable"
  | "graph_serialize_failed"
  | "graph_events_unavailable"
  | "graph_constructor_unavailable"
  | "graph_to_prompt_unavailable"
  | "load_api_json_unavailable"
  | "fetch_api_unavailable"
  | "file_url_unavailable"
  | "api_event_target_unavailable"
  | "ui_settings_unavailable"
  | "extension_registrar_unavailable"
  | "sidebar_tab_registration_unavailable"
  | "sidebar_tab_unregistration_unavailable"
  | "workflow_store_unavailable"
  | "load_graph_data_unavailable"
  | "queue_callable_unreadable"
  | "queue_callable_missing"
  | "toast_unavailable"
  | "modal_keyboard_guard_unavailable"
  | "canvas_keyboard_guard_unavailable";

export type SeamResult<T> =
  | Readonly<{ status: "ready"; value: T }>
  | Readonly<{ status: "unavailable"; reason: SeamRefusal }>;

export function seamReady<T>(value: T): SeamResult<T> {
  return Object.freeze({ status: "ready" as const, value });
}

export function seamUnavailable(reason: SeamRefusal): SeamResult<never> {
  return Object.freeze({ status: "unavailable" as const, reason });
}

export type EventSourceSeam = Readonly<{
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
}>;

export type UiSettingsSeam = EventTarget & {
  getSettingValue(id: string): unknown;
  setSettingValueAsync?(id: string, value: unknown): Promise<unknown>;
};

/** The documented sidebar tab shape accepted by `registerSidebarTab`. */
export type SidebarTabDescriptor = {
  id: string;
  title: string;
  tooltip: string;
  icon: string;
  type: "custom";
  render(container: HTMLElement): void;
  destroy(): void;
};

export type SidebarTabSeam = Readonly<{
  /** The documented registration API (`registerSidebarTab`). */
  register(tab: SidebarTabDescriptor): void;
  /** Present only when the host exposes `unregisterSidebarTab`. */
  unregister: ((id: string) => void) | undefined;
  /** Present only when the host exposes `getSidebarTabs`. */
  enumerate: (() => Array<{ id: string }>) | undefined;
}>;

type GraphSeam = { serialize?: unknown; events?: unknown };
type GraphHost = { graph?: unknown };

function isEventSource(value: unknown): value is EventSourceSeam {
  return (
    value !== null &&
    (typeof value === "object" || typeof value === "function") &&
    typeof (value as { addEventListener?: unknown }).addEventListener ===
      "function" &&
    typeof (value as { removeEventListener?: unknown }).removeEventListener ===
      "function"
  );
}

// --- graph family (frontend.app.graph, .serialize, .events) ------------------------------

/**
 * The host graph object. The host creates its root graph after extension modules are
 * evaluated (frontend 1.51.9 returns `undefined` from `app.graph` until then, and a host
 * getter may throw); both are refusals the caller re-probes later, never exceptions.
 */
export function probeGraph(app: GraphHost): SeamResult<object> {
  let graph: unknown;
  try {
    graph = app.graph;
  } catch {
    return seamUnavailable("graph_unreadable");
  }
  return graph !== null && typeof graph === "object"
    ? seamReady(graph)
    : seamUnavailable("graph_unavailable");
}

/** The graph serializer bound to its graph, without calling it. */
export function probeGraphSerializer(
  app: GraphHost,
): SeamResult<() => unknown> {
  const graph = probeGraph(app);
  if (graph.status !== "ready") return graph;
  const serialize = (graph.value as GraphSeam).serialize;
  if (typeof serialize !== "function")
    return seamUnavailable("graph_serialize_unavailable");
  const owner = graph.value;
  return seamReady(() => (serialize as () => unknown).call(owner));
}

/** Serialize the visible graph, classifying a throwing serializer as a refusal. */
export function serializeGraph(app: GraphHost): SeamResult<unknown> {
  const serializer = probeGraphSerializer(app);
  if (serializer.status !== "ready") return serializer;
  try {
    return seamReady(serializer.value());
  } catch {
    return seamUnavailable("graph_serialize_failed");
  }
}

/**
 * Exact stand-in for the optional-chained read `app.graph?.serialize?.()`: `undefined` when
 * the seam is absent, and a throwing serializer propagates to the caller's transaction
 * classifier. Use `serializeGraph` where the caller wants a named refusal instead.
 */
export function readVisibleGraph(app: GraphHost): unknown {
  const serializer = probeGraphSerializer(app);
  return serializer.status === "ready" ? serializer.value() : undefined;
}

export function probeGraphEvents(app: GraphHost): SeamResult<EventSourceSeam> {
  const graph = probeGraph(app);
  if (graph.status !== "ready") return graph;
  const events = (graph.value as GraphSeam).events;
  return isEventSource(events)
    ? seamReady(events)
    : seamUnavailable("graph_events_unavailable");
}

/**
 * The live host graph class, for detached candidate construction.
 *
 * CRITICAL: construct from the live host graph class. Importing/bundling a second LiteGraph
 * runtime would split node registries and Pinia-scoped graph stores from the instance ComfyUI
 * will compile.
 */
export function probeGraphConstructor(
  app: GraphHost,
): SeamResult<new (serialized?: unknown) => unknown> {
  const graph = probeGraph(app);
  if (graph.status !== "ready") return graph;
  const constructor = (
    graph.value as { constructor?: new (serialized?: unknown) => unknown }
  ).constructor;
  return typeof constructor === "function" && constructor !== Object
    ? seamReady(constructor)
    : seamUnavailable("graph_constructor_unavailable");
}

// --- compile family (frontend.app.graph_to_prompt, frontend.app.load_api_json) -----------

/** `graphToPrompt` bound to the host app; call it with the same arguments as before. */
export function probeGraphToPrompt<Compiled>(app: {
  graphToPrompt?: (graph?: unknown) => Promise<Compiled> | Compiled;
}): SeamResult<(graph?: unknown) => Promise<Compiled> | Compiled> {
  const compile = app.graphToPrompt;
  return typeof compile === "function"
    ? seamReady(compile.bind(app))
    : seamUnavailable("graph_to_prompt_unavailable");
}

export function probeLoadApiJson(app: {
  loadApiJson?: unknown;
}): SeamResult<(prompt: Record<string, unknown>, name?: string) => unknown> {
  const load = app.loadApiJson;
  return typeof load === "function"
    ? seamReady(
        (
          load as (prompt: Record<string, unknown>, name?: string) => unknown
        ).bind(app),
      )
    : seamUnavailable("load_api_json_unavailable");
}

// --- host API family (frontend.api.fetch_api, .file_url, .event_target) -------------------

/**
 * `fetchApi` bound to the host api object.
 *
 * CRITICAL: bind before destructuring. `api.fetchApi` resolves the route through `this`, so
 * a detached reference reaches the wrong origin or throws.
 */
export function probeFetchApi<
  Fetch extends (...args: never[]) => unknown,
>(api: { fetchApi?: Fetch }): SeamResult<Fetch> {
  const fetchApi = api.fetchApi;
  return typeof fetchApi === "function"
    ? seamReady(fetchApi.bind(api) as Fetch)
    : seamUnavailable("fetch_api_unavailable");
}

export function probeFileUrl(api: {
  fileURL?: (route: string) => string;
}): SeamResult<(route: string) => string> {
  const fileURL = api.fileURL;
  return typeof fileURL === "function"
    ? seamReady(fileURL.bind(api))
    : seamUnavailable("file_url_unavailable");
}

export function probeApiEventTarget(api: unknown): SeamResult<EventSourceSeam> {
  return isEventSource(api)
    ? seamReady(api)
    : seamUnavailable("api_event_target_unavailable");
}

// --- UI settings and registration (frontend.app.ui_settings, .register_extension,
//     .extension_manager.*_sidebar_tab) ----------------------------------------------------

export function probeUiSettings(app: {
  ui?: { settings?: unknown };
}): SeamResult<UiSettingsSeam> {
  const settings = app.ui?.settings;
  return settings !== null &&
    typeof settings === "object" &&
    typeof (settings as { getSettingValue?: unknown }).getSettingValue ===
      "function" &&
    isEventSource(settings)
    ? seamReady(settings as UiSettingsSeam)
    : seamUnavailable("ui_settings_unavailable");
}

export function probeExtensionRegistrar(app: {
  registerExtension?: unknown;
}): SeamResult<(extension: Record<string, unknown>) => void> {
  const register = app.registerExtension;
  return typeof register === "function"
    ? seamReady(
        (register as (extension: Record<string, unknown>) => void).bind(app),
      )
    : seamUnavailable("extension_registrar_unavailable");
}

/**
 * The sidebar-tab family. Registration requires the documented `registerSidebarTab`;
 * `getSidebarTabs` and `unregisterSidebarTab` are observed-but-undocumented and therefore
 * optional: when absent, the owner falls back to its own registration token (never to an
 * invented enumeration) and names the missing member on disposal.
 */
export function probeSidebarTabManager(app: {
  extensionManager?: unknown;
}): SeamResult<SidebarTabSeam> {
  const manager = app.extensionManager;
  if (manager === null || typeof manager !== "object")
    return seamUnavailable("sidebar_tab_registration_unavailable");
  const members = manager as {
    registerSidebarTab?: unknown;
    unregisterSidebarTab?: unknown;
    getSidebarTabs?: unknown;
  };
  if (typeof members.registerSidebarTab !== "function")
    return seamUnavailable("sidebar_tab_registration_unavailable");
  return seamReady(
    Object.freeze({
      register: (
        members.registerSidebarTab as (tab: SidebarTabDescriptor) => void
      ).bind(manager),
      unregister:
        typeof members.unregisterSidebarTab === "function"
          ? (members.unregisterSidebarTab as (id: string) => void).bind(manager)
          : undefined,
      enumerate:
        typeof members.getSidebarTabs === "function"
          ? (members.getSidebarTabs as () => Array<{ id: string }>).bind(
              manager,
            )
          : undefined,
    }),
  );
}

// --- host notifications (frontend.app.extension_manager.toast) -----------------------------

export type ToastMessage = Readonly<{
  severity: "success" | "info" | "warn" | "error";
  summary: string;
  detail?: string;
  life?: number;
}>;

/**
 * The host toast, when present. A notification is never load-bearing, so an absent toast is
 * a silent refusal the caller drops; this surface is not a census seam and nothing may
 * branch on it.
 */
export function probeToast(app: {
  extensionManager?: unknown;
}): SeamResult<(message: ToastMessage) => void> {
  const manager = app.extensionManager;
  const toast =
    manager !== null && typeof manager === "object"
      ? (manager as { toast?: unknown }).toast
      : undefined;
  const add =
    toast !== null && typeof toast === "object"
      ? (toast as { add?: unknown }).add
      : undefined;
  return typeof add === "function"
    ? seamReady((add as (message: ToastMessage) => void).bind(toast))
    : seamUnavailable("toast_unavailable");
}

// --- LiteGraph registry (frontend.litegraph.registered_node_types) ------------------------

/**
 * The raw node-definition registry, read lazily and without classification: the extension
 * module is evaluated before ComfyUI finishes registering every LiteGraph node class on the
 * supported host, and `classifyHostMappingReadiness` decides present-but-unready from the
 * raw value together with the readiness point.
 */
export function readNodeDefinitionRegistry(): unknown {
  return (
    globalThis as unknown as {
      LiteGraph?: { registered_node_types?: unknown };
    }
  ).LiteGraph?.registered_node_types;
}
