import type { HostSeamObservationWire } from "./hostSeamObservation";

export class HostTransportUnavailableError extends Error {
  constructor() {
    super("supplied host transport was unavailable");
    this.name = "HostTransportUnavailableError";
  }
}

export class HostProbePolicyError extends Error {
  constructor() {
    super("supplied host probe policy rejected the response");
    this.name = "HostProbePolicyError";
  }
}

export function isHostTransportUnavailable(
  error: unknown,
): error is HostTransportUnavailableError {
  return error instanceof HostTransportUnavailableError;
}

export function frontendProbeReadiness(
  stableSamples: number,
): "READY" | "UNAVAILABLE" {
  if (!Number.isSafeInteger(stableSamples) || stableSamples < 0)
    throw new Error("frontend stability sample count is invalid");
  return stableSamples >= 8 ? "READY" : "UNAVAILABLE";
}

export function requireHostHttpAvailability(status: number): void {
  if (!Number.isSafeInteger(status) || status < 100 || status > 599)
    throw new HostProbePolicyError();
  if (status === 200) return;
  if (status >= 300 && status < 400) throw new HostProbePolicyError();
  throw new HostTransportUnavailableError();
}

export type FrontendHostSeamProbeResult = Readonly<{
  observations: readonly HostSeamObservationWire[];
  nodeDefinitionCount: number;
  inspectionMilliseconds: number;
  allReady: boolean;
}>;

export async function observeFrontendHostSeamsInPage(): Promise<FrontendHostSeamProbeResult> {
  type Observation = {
    seam_id: string;
    presence: "present" | "absent";
    kind: "callable" | "collection" | "event_target" | "mapping" | "object";
    key_shape: "closed_members" | "node_type" | "none";
    element_kind: "callable" | "node_definition_wrapper" | "none" | "object";
    readiness_state: "ready" | "absent" | "unavailable";
    count_bucket: "none" | "one" | "tens" | "thousands";
    byte_bucket: "not_measured";
    latency_bucket: "sub_10ms";
  };
  const readiness = (
    available: boolean,
  ): Pick<Observation, "presence" | "readiness_state"> =>
    available
      ? { presence: "present", readiness_state: "ready" }
      : { presence: "absent", readiness_state: "absent" };
  const callable = (seamId: string, value: unknown): Observation => ({
    seam_id: seamId,
    ...readiness(typeof value === "function"),
    kind: "callable",
    key_shape: "none",
    element_kind: "none",
    count_bucket: "one",
    byte_bucket: "not_measured",
    latency_bucket: "sub_10ms",
  });
  const eventTarget = (seamId: string, value: unknown): Observation => {
    const candidate =
      value !== null && typeof value === "object"
        ? (value as Record<string, unknown>)
        : undefined;
    const available =
      typeof candidate?.addEventListener === "function" &&
      typeof candidate.removeEventListener === "function";
    return {
      seam_id: seamId,
      ...readiness(available),
      kind: "event_target",
      key_shape: "closed_members",
      element_kind: "callable",
      count_bucket: "one",
      byte_bucket: "not_measured",
      latency_bucket: "sub_10ms",
    };
  };
  const objectShape = (
    seamId: string,
    value: unknown,
    callableMembers: readonly string[],
    elementKind: "callable" | "none",
  ): Observation => {
    const candidate =
      value !== null && typeof value === "object"
        ? (value as Record<string, unknown>)
        : undefined;
    const available =
      candidate !== undefined &&
      callableMembers.every(
        (member) => typeof candidate[member] === "function",
      );
    return {
      seam_id: seamId,
      ...readiness(available),
      kind: "object",
      key_shape: "closed_members",
      element_kind: elementKind,
      count_bucket: "one",
      byte_bucket: "not_measured",
      latency_bucket: "sub_10ms",
    };
  };
  const collectionShape = (seamId: string, value: unknown): Observation => {
    const available =
      Array.isArray(value) &&
      value.every(
        (item) =>
          item !== null && typeof item === "object" && !Array.isArray(item),
      );
    const count = Array.isArray(value) ? value.length : 0;
    const countBucket =
      count === 0
        ? "none"
        : count === 1
          ? "one"
          : count < 1000
            ? "tens"
            : "thousands";
    return {
      seam_id: seamId,
      ...readiness(available),
      kind: "collection",
      key_shape: "none",
      element_kind: "object",
      count_bucket: countBucket,
      byte_bucket: "not_measured",
      latency_bucket: "sub_10ms",
    };
  };
  const importModule = (url: string): Promise<Record<string, unknown>> =>
    import(url) as Promise<Record<string, unknown>>;
  const [appModule, apiModule] = await Promise.all([
    importModule("/scripts/app.js"),
    importModule("/scripts/api.js"),
  ]);
  const app = appModule.app as Record<string, unknown>;
  const api = apiModule.api as Record<string, unknown>;
  const started = performance.now();
  const dataDescriptor = (
    value: unknown,
    name: string,
  ): PropertyDescriptor | undefined => {
    for (let depth = 0; depth < 8; depth += 1) {
      if (
        value === null ||
        (typeof value !== "object" && typeof value !== "function")
      )
        return undefined;
      const descriptor = Reflect.getOwnPropertyDescriptor(value, name);
      if (descriptor !== undefined)
        return "value" in descriptor ? descriptor : undefined;
      value = Reflect.getPrototypeOf(value);
    }
    return undefined;
  };
  const keyboardShape = (inspect: () => boolean): boolean => {
    try {
      return inspect();
    } catch {
      return false;
    }
  };
  // Each capability is independent: a throwing foreign canvas descriptor must not
  // turn a valid modal predicate into an invented missing capability, or vice versa.
  const modalKeyboardReady = keyboardShape(() => {
    const constructor = dataDescriptor(app, "constructor")?.value;
    const predicate =
      typeof constructor === "function"
        ? Reflect.getOwnPropertyDescriptor(constructor, "maskeditor_is_opended")
        : undefined;
    return (
      predicate !== undefined &&
      "value" in predicate &&
      predicate.configurable === true &&
      predicate.writable === true &&
      (predicate.value === null || typeof predicate.value === "function")
    );
  });
  const canvasKeyboardReady = keyboardShape(() => {
    const owner = dataDescriptor(app, "canvas")?.value;
    const element = dataDescriptor(owner, "canvas")?.value;
    const key = dataDescriptor(owner, "_key_callback")?.value;
    const ghost = dataDescriptor(owner, "_ghostKeyHandler")?.value;
    const ghostId = dataDescriptor(
      dataDescriptor(owner, "state")?.value,
      "ghostNodeId",
    )?.value;
    return (
      element instanceof HTMLCanvasElement &&
      element.ownerDocument === document &&
      document.defaultView !== null &&
      dataDescriptor(owner, "_events_binded")?.value === true &&
      typeof key === "function" &&
      ((ghost === null && ghostId === null) ||
        (typeof ghost === "function" &&
          (typeof ghostId === "string" ||
            (typeof ghostId === "number" && Number.isFinite(ghostId)))))
    );
  });
  const graph = app.graph as Record<string, unknown> | undefined;
  const extensionManager = app.extensionManager as
    Record<string, unknown> | undefined;
  const workflowStore = extensionManager?.workflow as
    Record<string, unknown> | undefined;
  const ui = app.ui as Record<string, unknown> | undefined;
  const registry = (
    globalThis as unknown as {
      LiteGraph?: { registered_node_types?: Record<string, unknown> };
    }
  ).LiteGraph?.registered_node_types;
  const registryReady =
    registry !== null &&
    typeof registry === "object" &&
    Object.keys(registry).length > 0 &&
    Object.values(registry).every(
      (value) =>
        value !== null &&
        (typeof value === "object" || typeof value === "function"),
    );
  const observations: Observation[] = [
    callable(
      "frontend.app.canvas.keyboard_capture",
      canvasKeyboardReady ? () => undefined : null,
    ),
    callable(
      "frontend.app.modal_keyboard_guard",
      modalKeyboardReady ? () => undefined : null,
    ),
    eventTarget("frontend.api.event_target", api),
    callable("frontend.api.fetch_api", api.fetchApi),
    callable("frontend.api.file_url", api.fileURL),
    callable("frontend.api.queue_prompt", api.queuePrompt),
    callable(
      "frontend.app.extension_manager.get_sidebar_tabs",
      extensionManager?.getSidebarTabs,
    ),
    callable(
      "frontend.app.extension_manager.register_sidebar_tab",
      extensionManager?.registerSidebarTab,
    ),
    callable(
      "frontend.app.extension_manager.unregister_sidebar_tab",
      extensionManager?.unregisterSidebarTab,
    ),
    objectShape(
      "frontend.app.extension_manager.workflow.active_workflow",
      workflowStore?.activeWorkflow,
      [],
      "none",
    ),
    collectionShape(
      "frontend.app.extension_manager.workflow.open_workflows",
      workflowStore?.openWorkflows,
    ),
    objectShape("frontend.app.graph", graph, [], "none"),
    callable("frontend.app.graph.change", graph?.change),
    eventTarget("frontend.app.graph.events", graph?.events),
    callable("frontend.app.graph.get_node_by_id", graph?.getNodeById),
    callable("frontend.app.graph.serialize", graph?.serialize),
    callable("frontend.app.graph.set_dirty_canvas", graph?.setDirtyCanvas),
    callable("frontend.app.graph_to_prompt", app.graphToPrompt),
    callable("frontend.app.load_api_json", app.loadApiJson),
    callable("frontend.app.load_graph_data", app.loadGraphData),
    callable("frontend.app.register_extension", app.registerExtension),
    objectShape(
      "frontend.app.ui_settings",
      ui?.settings,
      ["getSettingValue", "setSettingValue"],
      "callable",
    ),
    {
      seam_id: "frontend.litegraph.registered_node_types",
      ...readiness(registryReady),
      kind: "mapping",
      key_shape: "node_type",
      element_kind: "node_definition_wrapper",
      count_bucket: "thousands",
      byte_bucket: "not_measured",
      latency_bucket: "sub_10ms",
    },
  ];
  // The strict classifier requires canonical collector order; fixture normalization
  // would mask newly inserted rows arriving out of order. Sort only this owned array.
  observations.sort((left, right) =>
    left.seam_id < right.seam_id ? -1 : left.seam_id > right.seam_id ? 1 : 0,
  );
  return {
    observations,
    nodeDefinitionCount:
      registry === undefined ? 0 : Object.keys(registry).length,
    inspectionMilliseconds: performance.now() - started,
    allReady:
      registryReady &&
      observations.every(
        (row) => row.presence === "present" && row.readiness_state === "ready",
      ),
  };
}

export function buildBackendHostSeamObservations(
  input: Readonly<{
    objectInfo: Readonly<Record<string, unknown>>;
    generationProfile: Readonly<Record<string, unknown>>;
    generationProfileStatus: number;
    extensionBundleManifestEntries: number;
  }>,
): readonly HostSeamObservationWire[] {
  const present = (
    seamId: string,
    ready: boolean,
    shape: Omit<
      HostSeamObservationWire,
      "seam_id" | "presence" | "readiness_state"
    >,
  ): HostSeamObservationWire => ({
    seam_id: seamId,
    presence: ready ? "present" : "absent",
    readiness_state: ready ? "ready" : "absent",
    ...shape,
  });
  const shippedNodeIds = Object.keys(input.objectInfo).filter((id) =>
    id.startsWith("comfyui_h3_context."),
  );
  const shippedDisplayNamesReady = shippedNodeIds.every((id) => {
    const row = input.objectInfo[id] as Record<string, unknown>;
    return typeof row.display_name === "string";
  });
  const nativeH3RowsReady = [
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
  ].every((id) => Object.hasOwn(input.objectInfo, id));
  const generationProfileReady =
    input.generationProfile.schema === "h3.context.generation_profile.v1" &&
    Array.isArray(input.generationProfile.families) &&
    input.generationProfile.families.length > 0;
  return Object.freeze([
    present("backend.comfy_api.latest", nativeH3RowsReady, {
      kind: "module",
      key_shape: "closed_members",
      element_kind: "callable",
      count_bucket: "tens",
      byte_bucket: "not_measured",
      latency_bucket: "sub_10ms",
    }),
    present("backend.folder_paths.get_filename_list", generationProfileReady, {
      kind: "callable",
      key_shape: "none",
      element_kind: "path_string",
      count_bucket: "one",
      byte_bucket: "not_measured",
      latency_bucket: "sub_100ms",
    }),
    present("backend.node_class_mappings", shippedNodeIds.length > 0, {
      kind: "mapping",
      key_shape: "node_type",
      element_kind: "node_class",
      count_bucket: "tens",
      byte_bucket: "not_measured",
      latency_bucket: "sub_10ms",
    }),
    present(
      "backend.node_display_name_mappings",
      shippedNodeIds.length > 0 && shippedDisplayNamesReady,
      {
        kind: "mapping",
        key_shape: "node_type",
        element_kind: "display_name",
        count_bucket: "tens",
        byte_bucket: "not_measured",
        latency_bucket: "sub_10ms",
      },
    ),
    present(
      "backend.prompt_server.routes",
      input.generationProfileStatus === 200 && generationProfileReady,
      {
        kind: "route_registry",
        key_shape: "closed_members",
        element_kind: "route",
        count_bucket: "tens",
        byte_bucket: "not_measured",
        latency_bucket: "sub_10ms",
      },
    ),
    present(
      "backend.web_directory",
      input.extensionBundleManifestEntries === 1,
      {
        kind: "text",
        key_shape: "none",
        element_kind: "none",
        count_bucket: "one",
        byte_bucket: "sub_1kb",
        latency_bucket: "sub_10ms",
      },
    ),
  ]);
}
