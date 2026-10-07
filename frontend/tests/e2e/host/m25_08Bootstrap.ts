export type M2508PromptNode = Readonly<{
  class_type: string;
  inputs: Readonly<Record<string, unknown>>;
}>;

export type M2508Prompt = Readonly<Record<string, M2508PromptNode>>;

export type M2508HostBootstrap = Readonly<{
  visiblePrompt: M2508Prompt;
  executionPrompt: M2508Prompt;
  productShellNodeId: string;
  nativeAnchorNodeId: string;
}>;

export const M2508_OWNED_NODE_IDS = Object.freeze([
  "comfyui_h3_context.H3Context.ReferenceRegistry",
  "comfyui_h3_context.H3Context.ProductShell",
] as const);

const EXACT_ACTIVE_PYTHON_MODULE = "custom_nodes.ComfyUI-MiniMaxH3-Context";

const PRODUCT_SHELL_NODE_ID = "8";
const NATIVE_ANCHOR_NODE_ID = "9";
const DURATION_SOURCE_NODE_ID = "10";
const LENGTH_EXPRESSION_NODE_ID = "11";
const AUTHORIZED_VIDEO_LOCATOR =
  /^m25_08_acceptance\/[a-f0-9]{8,64}\/m25_08_authorized_video_v1\.mp4$/;

export const M2508_AUTHORIZED_MEDIA_RUNTIME_ROUTE =
  "/h3-context/v1/authoring/media-preview";

const M2508_AUTHORIZED_MEDIA_RUNTIME_REQUEST_ID =
  "m25-08-authorized-media-runtime";

type M2508WorkflowStore = {
  activeWorkflow?: unknown;
  openWorkflows?: unknown;
};

type M2508HostApp = {
  extensionManager?: { workflow?: M2508WorkflowStore };
  graph: { serialize(): unknown };
  rootGraph?: {
    constructor?: new () => {
      add(node: unknown): void;
      arrange(): void;
      getNodeById(id: string | number): unknown;
      serialize(): unknown;
    };
    serialize(): unknown;
  };
  canvas?: { graph?: unknown };
  loadApiJson(prompt: unknown, target: string | object): Promise<void>;
  loadGraphData(
    graph: unknown,
    clean: boolean,
    restoreView: boolean,
    workflow: object,
  ): Promise<void>;
};

export function isM2508HostGraphLifecycleReady(): boolean {
  // CRITICAL: sidebar/workflow registration can precede ComfyUI's canvas; a graph write in that
  // interval calls viewport persistence and fails with `getCanvas: canvas is null`.
  const app = (
    window as unknown as {
      comfyAPI?: {
        app?: {
          app?: { graph?: { serialize?: unknown }; canvas?: unknown };
        };
      };
    }
  ).comfyAPI?.app?.app;
  return (
    typeof app?.graph?.serialize === "function" &&
    app.canvas !== null &&
    app.canvas !== undefined
  );
}

export async function ensureM2508CapturedWorkflowAuthority(): Promise<object> {
  // CRITICAL: Playwright serializes this callback into the browser realm; keep every dependency
  // inside the function or supplied-host acceptance fails before it can inspect workflow state.
  const app = (
    window as unknown as {
      comfyAPI: { app: { app: M2508HostApp } };
    }
  ).comfyAPI.app.app;
  const workflowStore = app.extensionManager?.workflow;
  const openWorkflows = workflowStore?.openWorkflows;
  const active = workflowStore?.activeWorkflow;
  if (!Array.isArray(openWorkflows))
    throw new Error("M25-08 active workflow authority is unavailable");
  // CRITICAL: null/empty must be materialized by the single loadApiJson lifecycle below. Creating
  // an empty authority here preserves a stale empty tracker and makes the live graph disappear.
  if (active === null && openWorkflows.length === 0)
    throw new Error("M25-08 workflow authority is not materialized");
  if (active !== null && typeof active === "object" && !Array.isArray(active)) {
    if (!openWorkflows.includes(active))
      throw new Error("M25-08 workflow authority state is inconsistent");
    return active;
  }
  throw new Error("M25-08 workflow authority state is inconsistent");
}

export async function materializeM2508VisiblePromptInCapturedWorkflow(
  promptValue: unknown,
): Promise<void> {
  // CRITICAL: this is an independently serialized Playwright callback; importing or calling the
  // sibling helper leaves an unresolved module symbol in the browser and skips materialization.
  const app = (
    window as unknown as {
      comfyAPI: { app: { app: M2508HostApp } };
    }
  ).comfyAPI.app.app;
  const workflowStore = app.extensionManager?.workflow;
  const initialOpenWorkflows = workflowStore?.openWorkflows;
  const initialActive = workflowStore?.activeWorkflow;
  if (!Array.isArray(initialOpenWorkflows))
    throw new Error("M25-08 active workflow authority is unavailable");
  let authority: object | null;
  if (
    initialActive !== null &&
    typeof initialActive === "object" &&
    !Array.isArray(initialActive)
  ) {
    if (!initialOpenWorkflows.includes(initialActive))
      throw new Error("M25-08 workflow authority state is inconsistent");
    authority = initialActive;
  } else {
    if (initialActive !== null || initialOpenWorkflows.length !== 0)
      throw new Error("M25-08 workflow authority state is inconsistent");
    authority = null;
  }
  const openWorkflows = workflowStore?.openWorkflows;
  if (
    !Array.isArray(openWorkflows) ||
    (authority !== null && !openWorkflows.includes(authority))
  )
    throw new Error("M25-08 active workflow authority is unavailable");
  const openCount = openWorkflows.length;
  const expectedPath =
    authority === null
      ? "workflows/m25-08-authorized-video-acceptance.json"
      : (authority as { path?: unknown }).path;
  const assertAuthorityStable = () => {
    const finalAuthority = workflowStore?.activeWorkflow;
    const finalOpen = workflowStore?.openWorkflows;
    const expectedOpenCount = authority === null ? 1 : openCount;
    const finalAuthorityObject =
      finalAuthority !== null &&
      typeof finalAuthority === "object" &&
      !Array.isArray(finalAuthority);
    const receipt = {
      finalAuthorityObject,
      identityStable: authority === null || finalAuthority === authority,
      openArray: Array.isArray(finalOpen),
      openCount: Array.isArray(finalOpen) ? finalOpen.length : -1,
      expectedOpenCount,
      activeIsMember:
        finalAuthorityObject &&
        Array.isArray(finalOpen) &&
        finalOpen.includes(finalAuthority),
      pathExact:
        finalAuthorityObject &&
        (finalAuthority as { path?: unknown }).path === expectedPath,
    };
    if (
      !receipt.finalAuthorityObject ||
      !receipt.identityStable ||
      !receipt.openArray ||
      receipt.openCount !== receipt.expectedOpenCount ||
      !receipt.activeIsMember ||
      !receipt.pathExact
    )
      throw new Error(
        `M25-08 workflow authority changed: ${JSON.stringify(receipt)}`,
      );
  };

  if (authority === null) {
    // CRITICAL: on a fresh host, let loadApiJson create the only public workflow and bind it to the
    // graph. Pre-opening an empty workflow preserves a stale tracker and loses visible nodes.
    await app.loadApiJson(
      promptValue,
      "m25-08-authorized-video-acceptance.json",
    );
  } else {
    // CRITICAL: ComfyUI 1.51.9 restores the old tracker after loadApiJson targets an existing
    // workflow, erasing the converted root graph. Build with its public LiteGraph constructors and
    // use the same captured-workflow loadGraphData seam as production; do not create a second tab.
    const GraphConstructor = app.rootGraph?.constructor;
    const liteGraph = (
      globalThis as unknown as {
        LiteGraph?: { createNode?: (type: string) => unknown };
      }
    ).LiteGraph;
    if (
      typeof GraphConstructor !== "function" ||
      typeof liteGraph?.createNode !== "function" ||
      typeof app.loadGraphData !== "function"
    )
      throw new Error("M25-08 detached graph materialization is unavailable");
    const detached = new GraphConstructor();
    const prompt = promptValue as Record<
      string,
      { class_type?: unknown; inputs?: unknown }
    >;
    const nodeId = (id: string): string | number =>
      /^-?\d+$/.test(id) ? Number(id) : id;
    for (const [id, value] of Object.entries(prompt)) {
      if (typeof value?.class_type !== "string")
        throw new Error("M25-08 visible prompt node type is invalid");
      const node = liteGraph.createNode(value.class_type) as
        { id?: string | number } | null | undefined;
      if (node === null || node === undefined)
        throw new Error(
          `M25-08 visible node is unavailable: ${value.class_type}`,
        );
      node.id = nodeId(id);
      detached.add(node);
    }
    const bindInputs = (id: string, connectLinks: boolean) => {
      const value = prompt[id];
      const node = detached.getNodeById(nodeId(id)) as {
        inputs?: Array<{ name?: unknown; link?: unknown }>;
        widgets?: Array<{
          name?: unknown;
          value?: unknown;
          callback?: (value: unknown) => unknown;
        }>;
        convertWidgetToInput?: (widget: unknown) => boolean;
      } | null;
      if (value === undefined || node === null)
        throw new Error("M25-08 detached graph node identity is unavailable");
      const inputs =
        value.inputs !== null &&
        typeof value.inputs === "object" &&
        !Array.isArray(value.inputs)
          ? (value.inputs as Record<string, unknown>)
          : {};
      for (const [name, inputValue] of Object.entries(inputs)) {
        if (Array.isArray(inputValue)) {
          if (!connectLinks) continue;
          const linkValues =
            inputValue.length === 2 &&
            (typeof inputValue[0] === "string" ||
              typeof inputValue[0] === "number") &&
            typeof inputValue[1] === "number"
              ? [inputValue as [string | number, number]]
              : inputValue.every(
                    (candidate) =>
                      Array.isArray(candidate) &&
                      candidate.length === 2 &&
                      (typeof candidate[0] === "string" ||
                        typeof candidate[0] === "number") &&
                      typeof candidate[1] === "number",
                  )
                ? (inputValue as Array<[string | number, number]>)
                : null;
          if (linkValues === null)
            throw new Error("M25-08 detached graph link is invalid");
          for (const [sourceId, sourceSlot] of linkValues) {
            const source = detached.getNodeById(
              typeof sourceId === "string" ? nodeId(sourceId) : sourceId,
            ) as { connect?: (...args: unknown[]) => unknown } | null;
            let targetSlot =
              node.inputs?.findIndex(
                (input) =>
                  (input.name === name ||
                    (typeof input.name === "string" &&
                      input.name.startsWith(`${name}.`))) &&
                  (input.link === null || input.link === undefined),
              ) ?? -1;
            if (targetSlot === -1) {
              const widget = node.widgets?.find(
                (candidate) => candidate.name === name,
              );
              if (widget !== undefined && node.convertWidgetToInput?.(widget))
                targetSlot =
                  node.inputs?.findIndex((input) => input.name === name) ?? -1;
            }
            if (
              source === null ||
              typeof source?.connect !== "function" ||
              targetSlot === -1
            )
              throw new Error(
                `M25-08 detached graph link endpoint is unavailable: ${JSON.stringify(
                  {
                    nodeId: id,
                    name,
                    sourceId,
                    sourceSlot,
                    targetInputs: (node.inputs ?? []).map(
                      (input) => input.name,
                    ),
                  },
                )}`,
              );
            source.connect(sourceSlot, node, targetSlot);
          }
        } else {
          const widget = node.widgets?.find(
            (candidate) => candidate.name === name,
          );
          if (widget !== undefined) {
            widget.value = inputValue;
            widget.callback?.(inputValue);
          }
        }
      }
    };
    for (const id of Object.keys(prompt)) bindInputs(id, true);
    detached.arrange();
    for (const id of Object.keys(prompt)) bindInputs(id, false);
    detached.arrange();
    await app.loadGraphData(detached.serialize(), true, true, authority);
  }
  // IMPORTANT: the host facade can publish the open tab one task before activeWorkflow. Reading
  // between those writes falsely reports null authority and was the live-only bootstrap seam.
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const finalActive = workflowStore?.activeWorkflow;
    if (
      finalActive !== null &&
      typeof finalActive === "object" &&
      !Array.isArray(finalActive)
    )
      break;
    await new Promise<void>((resolvePromise) =>
      setTimeout(() => resolvePromise(), 25),
    );
  }
  assertAuthorityStable();

  const graph = app.graph.serialize();
  const rootGraph = app.rootGraph?.serialize() ?? graph;
  const nodesOf = (value: unknown): Array<Record<string, unknown>> =>
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Array.isArray((value as { nodes?: unknown }).nodes)
      ? ((value as { nodes: unknown[] }).nodes as Array<
          Record<string, unknown>
        >)
      : [];
  const actualNodes = nodesOf(graph);
  const rootNodes = nodesOf(rootGraph);
  const expectedNodes =
    promptValue !== null &&
    typeof promptValue === "object" &&
    !Array.isArray(promptValue)
      ? Object.entries(promptValue as Record<string, unknown>)
      : [];
  const exactProjection = expectedNodes.every(([id, value]) => {
    const expectedType =
      value !== null && typeof value === "object" && !Array.isArray(value)
        ? (value as { class_type?: unknown }).class_type
        : undefined;
    return actualNodes.some(
      (node) => String(node.id) === id && node.type === expectedType,
    );
  });
  if (expectedNodes.length === 0 || !exactProjection) {
    // IMPORTANT: loadApiJson always writes rootGraph, while a restored subgraph can leave app.graph
    // elsewhere; keep both bounded summaries or a live-only projection failure is not diagnosable.
    const summarize = (nodes: Array<Record<string, unknown>>) =>
      nodes.map((node) => ({ id: node.id, type: node.type }));
    throw new Error(
      `M25-08 visible graph materialization is incomplete: ${JSON.stringify({
        expected: expectedNodes.map(([id, value]) => ({
          id,
          type:
            value !== null && typeof value === "object" && !Array.isArray(value)
              ? (value as { class_type?: unknown }).class_type
              : undefined,
        })),
        current: summarize(actualNodes),
        root: summarize(rootNodes),
        activeState: summarize(
          nodesOf(
            (workflowStore?.activeWorkflow as { activeState?: unknown } | null)
              ?.activeState,
          ),
        ),
        currentIsRoot: app.graph === app.rootGraph,
        canvasUsesRoot: app.canvas?.graph === app.rootGraph,
        detachedGraphConstructor: typeof (
          app.rootGraph as { constructor?: unknown } | undefined
        )?.constructor,
        rootAdd: typeof (app.rootGraph as { add?: unknown } | undefined)?.add,
        liteGraphCreateNode: typeof (
          globalThis as unknown as {
            LiteGraph?: { createNode?: unknown };
          }
        ).LiteGraph?.createNode,
      })}`,
    );
  }
}

function clonePrompt(prompt: M2508Prompt): Record<string, M2508PromptNode> {
  return JSON.parse(JSON.stringify(prompt)) as Record<string, M2508PromptNode>;
}

export function assertM2508ExactHostCapability(
  nodeId: (typeof M2508_OWNED_NODE_IDS)[number],
  wire: unknown,
): void {
  const root =
    wire !== null && typeof wire === "object" && !Array.isArray(wire)
      ? (wire as Record<string, unknown>)
      : null;
  const node = root?.[nodeId];
  const pythonModule =
    node !== null && typeof node === "object" && !Array.isArray(node)
      ? (node as Record<string, unknown>).python_module
      : null;
  // CRITICAL: source-byte parity does not prove which duplicate custom-node directory won stable
  // ID registration. A backup-suffixed owner must abort before the one authorized queue call.
  if (pythonModule !== EXACT_ACTIVE_PYTHON_MODULE)
    throw new Error("M25-08 active node module is not exact");
}

export function buildM2508AuthorizedMediaRuntimeProbe(): Readonly<
  Record<string, unknown>
> {
  return Object.freeze({
    schema: "h3.context.authoring_source_preview.request.v1",
    requestId: M2508_AUTHORIZED_MEDIA_RUNTIME_REQUEST_ID,
    workspaceHandle: "m25-08-runtime-preflight-never-issued",
    referenceRevision: 1,
    timelineRevision: 1,
    timelineContentFingerprint: `sha256:${"0".repeat(64)}`,
    clipId: "m25-08-runtime-preflight",
  });
}

export function assertM2508AuthorizedMediaRuntimePrecondition(
  status: number,
  wire: unknown,
): void {
  const root =
    wire !== null && typeof wire === "object" && !Array.isArray(wire)
      ? (wire as Record<string, unknown>)
      : null;
  const exactKeys = ["reason", "requestId", "schema"];
  const actualKeys = root === null ? [] : Object.keys(root).sort();
  const closedError =
    JSON.stringify(actualKeys) === JSON.stringify(exactKeys) &&
    root?.schema === "h3.context.authoring_source_preview.error.v1" &&
    root.requestId === M2508_AUTHORIZED_MEDIA_RUNTIME_REQUEST_ID;

  // CRITICAL: route registration does not prove the process-local media adapter is published.
  // The one-shot host queue must stay untouched until this adapter-before-workspace ordering proves
  // runtime activation; otherwise a missing launch environment consumes the acceptance sample.
  if (closedError && status === 422 && root.reason === "unsupported")
    throw new Error("M25-08 authorized media runtime is unavailable");
  if (!(closedError && status === 404 && root.reason === "authority_mismatch"))
    throw new Error(
      "M25-08 authorized media runtime precondition is inconclusive",
    );
}

export function installM2508ManagedExecutionProjection(
  executionPromptValue: unknown,
): void {
  // CRITICAL: this callback is serialized into the browser realm. Keep the pure projection local
  // and exact; a post-fingerprint queue rewrite makes the validated and submitted prompts differ.
  const runtime = window as unknown as {
    __h3M2508ManagedExecutionProjection?: (
      compiled: Record<string, unknown>,
    ) => Record<string, unknown>;
    __h3M2508ExecutionIdentityTrace?: Array<Record<string, unknown>>;
    __h3ProjectionTrace?: Array<Record<string, unknown>>;
    __h3HostProjectionTrace?: Array<Record<string, unknown>>;
  };
  const executionPrompt =
    executionPromptValue !== null &&
    typeof executionPromptValue === "object" &&
    !Array.isArray(executionPromptValue)
      ? (executionPromptValue as Record<string, unknown>)
      : null;
  if (executionPrompt === null)
    throw new Error("M25-08 managed execution projection is unavailable");
  const expectedIds = Object.keys(executionPrompt).sort();
  const canonicalJson = (candidate: unknown): string | null => {
    const seen = new WeakSet<object>();
    let members = 0;
    const visit = (value: unknown, depth: number): string | null => {
      members += 1;
      if (members > 4096 || depth > 32) return null;
      if (value === null) return "null";
      if (typeof value === "string" || typeof value === "boolean")
        return JSON.stringify(value);
      if (typeof value === "number")
        return Number.isFinite(value) ? JSON.stringify(value) : null;
      if (typeof value !== "object") return null;
      if (seen.has(value)) return null;
      seen.add(value);
      if (Array.isArray(value)) {
        if (value.length > 512) return null;
        const parts = value.map((entry) => visit(entry, depth + 1));
        return parts.some((entry) => entry === null)
          ? null
          : `[${parts.join(",")}]`;
      }
      const object = value as Record<string, unknown>;
      const keys = Object.keys(object).sort();
      if (keys.length > 256) return null;
      const parts = keys.map((key) => {
        const encoded = visit(object[key], depth + 1);
        return encoded === null ? null : `${JSON.stringify(key)}:${encoded}`;
      });
      return parts.some((entry) => entry === null)
        ? null
        : `{${parts.join(",")}}`;
    };
    return visit(candidate, 0);
  };
  const expectedCanonical: Record<string, string> = {};
  if (
    expectedIds.length === 0 ||
    Object.entries(executionPrompt).some(([id, value]) => {
      const node =
        value !== null && typeof value === "object" && !Array.isArray(value)
          ? (value as Record<string, unknown>)
          : null;
      const classType = node?.class_type;
      if (typeof classType !== "string" || classType.startsWith("MiniMaxH3"))
        return true;
      const canonical = canonicalJson(value);
      if (canonical === null) return true;
      expectedCanonical[id] = canonical;
      return false;
    })
  )
    throw new Error("M25-08 model-free execution allowlist is invalid");

  runtime.__h3M2508ManagedExecutionProjection = (
    compiledValue: Record<string, unknown>,
  ): Record<string, unknown> => {
    const outputValue = compiledValue?.output;
    const output =
      outputValue !== null &&
      typeof outputValue === "object" &&
      !Array.isArray(outputValue)
        ? (outputValue as Record<string, unknown>)
        : null;
    if (output === null)
      throw new Error("M25-08 compiled execution output is unavailable");
    const compiledEntries = Object.entries(output);
    const nativeEntries = compiledEntries.filter(([, value]) => {
      const node =
        value !== null && typeof value === "object" && !Array.isArray(value)
          ? (value as Record<string, unknown>)
          : null;
      return String(node?.class_type ?? "").startsWith("MiniMaxH3");
    });
    if (
      nativeEntries.length !== 1 ||
      nativeEntries[0]?.[0] !== "9" ||
      (nativeEntries[0]?.[1] as Record<string, unknown> | undefined)
        ?.class_type !== "MiniMaxH3ReferenceToVideo"
    )
      throw new Error("M25-08 native execution boundary is not exact");
    const compiledIds = compiledEntries.map(([id]) => id).sort();
    if (
      JSON.stringify(compiledIds) !==
      JSON.stringify([...expectedIds, "9"].sort())
    )
      throw new Error("M25-08 model-free execution projection is not exact");
    const modelFreeOutput: Record<string, unknown> = {};
    for (const id of expectedIds) {
      const actualValue = output[id];
      const actual =
        actualValue !== null &&
        typeof actualValue === "object" &&
        !Array.isArray(actualValue)
          ? (actualValue as Record<string, unknown>)
          : null;
      const normalized =
        actual === null
          ? null
          : (structuredClone(actual) as Record<string, unknown>);
      if (normalized !== null && "_meta" in normalized) {
        const meta = normalized._meta;
        const metaKeys =
          meta !== null && typeof meta === "object" && !Array.isArray(meta)
            ? Object.keys(meta as Record<string, unknown>).sort()
            : [];
        if (
          JSON.stringify(metaKeys) !== JSON.stringify(["title"]) ||
          typeof (meta as { title?: unknown }).title !== "string" ||
          (meta as { title: string }).title.length > 256
        )
          throw new Error(
            "M25-08 model-free execution projection is not exact",
          );
        delete normalized._meta;
      }
      // CRITICAL: class/type equality is not an execution allowlist. Compare the complete bounded
      // JSON node after removing only ComfyUI's closed title metadata, so a same-class locator or
      // link rewrite cannot cross the pre-fingerprint seam.
      if (
        normalized === null ||
        canonicalJson(normalized) !== expectedCanonical[id]
      )
        throw new Error("M25-08 model-free execution projection is not exact");
      modelFreeOutput[id] = structuredClone(executionPrompt[id]);
    }
    return {
      ...structuredClone(compiledValue),
      output: modelFreeOutput,
    };
  };
  runtime.__h3M2508ExecutionIdentityTrace = [];
  runtime.__h3ProjectionTrace = [];
  runtime.__h3HostProjectionTrace = [];
}

export function buildM2508HostBootstrap(
  videoLocator: string,
): M2508HostBootstrap {
  // CRITICAL: this private binding is the only media locator accepted by the live row; widening
  // it would let an acceptance run select arbitrary host media or traverse outside input storage.
  if (videoLocator.length > 240 || !AUTHORIZED_VIDEO_LOCATOR.test(videoLocator))
    throw new Error("M25-08 VIDEO locator is not authorized");

  const visiblePrompt: M2508Prompt = {
    "1": {
      class_type: "comfyui_h3_context.H3Context.Request",
      inputs: {
        task_mode: "ref2va",
        user_intent:
          "Preserve the supplied synthetic reference video while the camera remains steady.",
        duration_seconds: [DURATION_SOURCE_NODE_ID, 0],
      },
    },
    "2": {
      class_type: "LoadVideo",
      inputs: { file: videoLocator },
    },
    "3": {
      class_type: "comfyui_h3_context.H3Context.ReferenceRegistry",
      // IMPORTANT: API prompt list sockets still take one direct ComfyUI link here; nesting it
      // makes host validation reject the precondition before ReferenceRegistry can execute.
      inputs: { videos: ["2", 0] },
    },
    "4": {
      class_type: "comfyui_h3_context.H3Context.Plan",
      inputs: {
        request: ["1", 0],
        reference_registry: ["3", 0],
      },
    },
    "5": {
      class_type: "comfyui_h3_context.H3Context.Compiler",
      inputs: { plan: ["4", 0] },
    },
    "6": {
      class_type: "comfyui_h3_context.H3Context.Validator",
      inputs: {
        plan: ["4", 0],
        prompt_document: ["5", 2],
      },
    },
    "7": {
      class_type: "comfyui_h3_context.H3Context.NativeH3Adapter",
      inputs: { report: ["6", 1] },
    },
    [PRODUCT_SHELL_NODE_ID]: {
      class_type: "comfyui_h3_context.H3Context.ProductShell",
      inputs: {
        report: ["6", 1],
        native_h3_wiring: ["7", 1],
      },
    },
    [NATIVE_ANCHOR_NODE_ID]: {
      class_type: "MiniMaxH3ReferenceToVideo",
      inputs: {
        prompt: [PRODUCT_SHELL_NODE_ID, 0],
        width: 512,
        height: 512,
        length: [LENGTH_EXPRESSION_NODE_ID, 1],
        ref_image_size: "match",
        ref_videos: [["2", 0]],
      },
    },
    [DURATION_SOURCE_NODE_ID]: {
      class_type: "PrimitiveFloat",
      inputs: { value: 4 },
    },
    [LENGTH_EXPRESSION_NODE_ID]: {
      class_type: "ComfyMathExpression",
      inputs: {
        expression:
          "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17",
        "values.a": [DURATION_SOURCE_NODE_ID, 0],
      },
    },
  };
  const executionPrompt = clonePrompt(visiblePrompt);
  delete executionPrompt[NATIVE_ANCHOR_NODE_ID];
  if (
    Object.values(executionPrompt).some((node) =>
      node.class_type.startsWith("MiniMaxH3"),
    )
  )
    throw new Error("M25-08 execution prompt contains native generation");

  return Object.freeze({
    visiblePrompt,
    executionPrompt,
    productShellNodeId: PRODUCT_SHELL_NODE_ID,
    nativeAnchorNodeId: NATIVE_ANCHOR_NODE_ID,
  });
}
