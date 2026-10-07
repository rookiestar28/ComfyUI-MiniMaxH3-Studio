import type { Page, Response, Route } from "@playwright/test";
import {
  decodeProductionWorkbenchProjection,
  type ProductionWorkbenchProjection,
} from "../../../src/contracts/productionWorkbenchCodec";
import type { ManagedSerialSequenceStartIntent } from "../../../src/host/managedSequenceRunnerContract";
import type { OwnedGraphReference } from "../../../src/host/ownedGraphIdentity";
import { decodeSequenceCoordinatorResult } from "../../../src/host/sequenceCoordinator";
import { normalizeM2508HostApiPath } from "./m25_08RequestClassification";

type Compiled = {
  output: Record<
    string,
    { class_type: string; inputs: Record<string, unknown> }
  >;
  [key: string]: unknown;
};

/** Self-contained so the exact tested function can also run in the host page. */
export function projectStockVideoExecution(
  compiled: Compiled,
  fixture: { inputFilename: string; outputPrefix: string },
): Compiled {
  if (
    !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,190}\.mp4$/.test(fixture.inputFilename) ||
    fixture.inputFilename.includes("..") ||
    !/^[A-Za-z0-9][A-Za-z0-9_-]{0,100}$/.test(fixture.outputPrefix)
  )
    throw new Error("stock producer fixture identity is not bounded");
  const clone = structuredClone(compiled);
  const nodes = clone.output;
  const rows = Object.entries(nodes);
  const shell = rows.filter(
    ([, node]) =>
      node.class_type === "comfyui_h3_context.H3Context.ProductShell",
  );
  const sinks = rows.filter(([, node]) => node.class_type === "SaveVideo");
  if (shell.length !== 1 || sinks.length !== 1 || rows.length > 512)
    throw new Error(
      "stock producer requires one original shell and video sink",
    );
  const bootstrap = new Set(
    [
      "Request",
      "Plan",
      "Compiler",
      "Validator",
      "NativeH3Adapter",
      "ProductShell",
      "ReferenceRegistry",
      "AuditOverride",
    ].map((name) => `comfyui_h3_context.H3Context.${name}`),
  );
  bootstrap.add("PrimitiveFloat");
  bootstrap.add("PrimitiveInt");
  const keep = new Set<string>();
  const visit = (id: string): void => {
    if (keep.has(id)) return;
    const node = nodes[id];
    if (!node || !bootstrap.has(node.class_type) || keep.size >= 128)
      throw new Error("stock producer shell closure is not model-free");
    keep.add(id);
    for (const value of Object.values(node.inputs)) {
      if (
        Array.isArray(value) &&
        value.length === 2 &&
        typeof value[1] === "number"
      ) {
        const source = String(value[0]);
        if (!Object.hasOwn(nodes, source))
          throw new Error("stock producer closure contains a dangling input");
        visit(source);
      }
    }
  };
  visit(shell[0]![0]);
  clone.output = Object.fromEntries(rows.filter(([id]) => keep.has(id)));
  let loaderId = "h3_runtime_video";
  for (let suffix = 1; Object.hasOwn(nodes, loaderId); suffix += 1)
    loaderId = `h3_runtime_video_${suffix}`;
  // IMPORTANT: keep the original sink ID before prevalidation. Changing it after the
  // queue fingerprint would detach real SaveVideo events from canonical capture authority.
  clone.output[loaderId] = {
    class_type: "LoadVideo",
    inputs: { file: fixture.inputFilename },
  };
  clone.output[sinks[0]![0]] = {
    class_type: "SaveVideo",
    inputs: {
      video: [loaderId, 0],
      filename_prefix: fixture.outputPrefix,
      format: "mp4",
      "format.codec": "h264",
      "format.codec.encoding": "re-encode",
      "format.codec.encoding.crf": 23,
    },
  };
  return clone;
}

export type StockSinkExecutionProof = Readonly<{
  promptId: string;
  outputNodeId: string;
  executingSequence: number;
  executedSequence: number;
}>;

/** Self-contained for the same browser serialization as the execution projector. */
export function createStockSinkExecutionTrace() {
  const events: {
    type: "executing" | "executed";
    promptId: string;
    outputNodeId: string;
    sequence: number;
  }[] = [];
  let overflow = false;
  return {
    observe(message: unknown, sink: string): void {
      const frame = message as {
        type?: unknown;
        data?: { prompt_id?: unknown; node?: unknown };
      } | null;
      if (
        !frame ||
        !["executing", "executed"].includes(String(frame.type)) ||
        typeof frame.data?.prompt_id !== "string" ||
        frame.data.prompt_id.length === 0 ||
        frame.data.prompt_id.length > 160 ||
        typeof frame.data.node !== "string" ||
        frame.data.node !== sink ||
        sink.length === 0 ||
        sink.length > 160
      )
        return;
      if (events.length >= 64) {
        overflow = true;
        return;
      }
      events.push({
        type: frame.type as "executing" | "executed",
        promptId: frame.data.prompt_id,
        outputNodeId: frame.data.node,
        sequence: events.length + 1,
      });
    },
    prove(
      queues: readonly { promptId: string; outputNodeId: string }[],
    ): StockSinkExecutionProof[] {
      if (
        overflow ||
        ![1, 2, 4].includes(queues.length) ||
        new Set(queues.map((row) => row.promptId)).size !== queues.length
      )
        throw new Error(
          "stock sink execution trace capacity or queue identity mismatch",
        );
      return queues.map((queue) => {
        const matched = events.filter(
          (event) =>
            event.promptId === queue.promptId &&
            event.outputNodeId === queue.outputNodeId,
        );
        // IMPORTANT: cached outputs also emit executed. Require a prior raw executing
        // frame for this exact prompt and original sink; UI node-only events lose identity.
        if (
          matched.length !== 2 ||
          matched[0].type !== "executing" ||
          matched[1].type !== "executed" ||
          matched[0].sequence >= matched[1].sequence
        )
          throw new Error(
            "stock sink lacks one ordered uncached execution proof",
          );
        return {
          promptId: queue.promptId,
          outputNodeId: queue.outputNodeId,
          executingSequence: matched[0].sequence,
          executedSequence: matched[1].sequence,
        };
      });
    },
  };
}

/** Self-contained for browser serialization: select the stock fixture for one real child.
 * The native `Generate approved sequence` path runs the product resolver, so the fixture is
 * chosen from the observed `prepare_sequence_child` request that precedes every resolve. */
export function selectStockChildFixture(
  expectedSegmentIds: readonly string[],
  queuedSegmentIds: readonly string[],
  payload: unknown,
): { index: number; segmentId: string } {
  const segmentId = (payload as { segment_id?: unknown } | null)?.segment_id;
  if (typeof segmentId !== "string")
    throw new Error("stock producer observed a malformed child preparation");
  const index = expectedSegmentIds.indexOf(segmentId);
  // IMPORTANT: preparation precedes the queue, so the next child is the queued count. A
  // retry re-prepares the segment that never queued; a skipped, reordered or already queued
  // segment is never admitted, or a fixture would be bound to the wrong real child.
  if (index < 0 || index !== queuedSegmentIds.length)
    throw new Error("stock producer child preparation is out of plan order");
  return { index, segmentId };
}

export type ProductionRuntimeProducerOptions = Readonly<{
  bundleUrl: string;
  workspaceHandle: string;
  /** The accepted plan's segment order that the real parent must execute. */
  segmentIds: readonly string[];
  ownedReference: OwnedGraphReference;
  inputFilename: string | readonly string[];
  runPrefix: string;
  timeoutMs?: number;
  /**
   * `direct` starts the parent from this helper (the accepted M26-05 two-child row). `native`
   * installs observation and projection only; the caller activates the product's own Start.
   */
  start: "direct" | "native";
}>;

export type ProductionRuntimeProducerResult = Readonly<{
  workspace: ProductionWorkbenchProjection;
  parentSequenceId: string;
  childPromptIds: readonly string[];
  sinkExecutionProofs: readonly StockSinkExecutionProof[];
  sinkExecutions: readonly {
    promptId: string;
    outputNodeId: string;
    locator: { filename: string; subfolder: string; type: "output" };
  }[];
  captureReceipts: readonly {
    promptId: string;
    outputNodeId: string;
    runHandle: string;
    receiptFingerprint: string;
    byteLength: number;
  }[];
  ownedReference: OwnedGraphReference;
}>;

export type ProductionRuntimeProducerHandle = Readonly<{
  /** Queue receipts and uncached sink completions observed so far; bounded identities only. */
  progress(): Promise<
    Readonly<{
      queued: readonly { promptId: string; segmentId: string }[];
      executedPromptIds: readonly string[];
    }>
  >;
  /** Wait for the parent to succeed, then join queues, executions and captures. */
  collect(timeoutMs?: number): Promise<ProductionRuntimeProducerResult>;
  /** Detach an unfinished parent, then remove every observer and the projector. Idempotent. */
  dispose(): Promise<void>;
}>;

/** Install real stock-encoding projection and capture observation for one Production parent.
 * Installation authorizes, starts and queues nothing. The caller owns workspace cancellation,
 * release and fixture cleanup, also on failure, and must call `dispose`.
 */
export async function installProductionRuntimeProducer(
  page: Page,
  options: ProductionRuntimeProducerOptions,
): Promise<ProductionRuntimeProducerHandle> {
  const { segmentIds } = options;
  if (
    ![1, 2, 4].includes(segmentIds.length) ||
    new Set(segmentIds).size !== segmentIds.length ||
    !/^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$/.test(options.runPrefix) ||
    !["direct", "native"].includes(options.start)
  )
    throw new Error(
      "stock producer must use the supplied bounded parent intent",
    );
  const inputFilenames =
    typeof options.inputFilename === "string"
      ? segmentIds.map(() => options.inputFilename as string)
      : [...options.inputFilename];
  if (
    inputFilenames.length !== segmentIds.length ||
    inputFilenames.some(
      (name) =>
        !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,190}\.mp4$/.test(name) ||
        name.includes(".."),
    )
  )
    throw new Error(
      "stock producer requires one bounded input fixture per child",
    );
  const captures: ProductionRuntimeProducerResult["captureReceipts"][number][] =
    [];
  const responseReads: Promise<void>[] = [];
  let observationFailure: unknown;
  const observeResponse = (response: Response): void => {
    if (
      normalizeM2508HostApiPath(new URL(response.url()).pathname) !==
        "/h3-context/v1/generation/coordinator" ||
      response.request().method() !== "POST"
    )
      return;
    const body = response.request().postDataJSON();
    if (body.action !== "record_artifact") return;
    responseReads.push(
      (async () => {
        if (!response.ok())
          throw new Error("real artifact capture request failed");
        const decoded = decodeSequenceCoordinatorResult(await response.json());
        if (
          !decoded.artifactAuthority ||
          decoded.runHandle !== body.payload.run_handle
        )
          throw new Error(
            "real artifact capture receipt is absent or mismatched",
          );
        captures.push({
          promptId: body.payload.queue_prompt_id,
          outputNodeId: body.payload.output_node_id,
          runHandle: decoded.runHandle,
          receiptFingerprint: decoded.artifactAuthority.receiptFingerprint,
          byteLength: decoded.artifactAuthority.byteLength,
        });
      })().catch((error: unknown) => {
        observationFailure = error;
      }),
    );
  };
  const routePattern = "**/h3-context/v1/managed-sequences";
  const observeBinding = async (route: Route): Promise<void> => {
    const request = route.request();
    if (request.method() === "POST") {
      const body = request.postDataJSON();
      if (body.action === "bind_prepared_child") {
        await page.evaluate((payload) => {
          const state = (window as any).__h3StockRuntimeProducer;
          if (!state || payload.segment_id !== state.segmentId)
            throw new Error("stock producer observed a foreign child binding");
          state.lastObservation = payload.observation;
        }, body.payload);
      } else if (
        options.start === "native" &&
        body.action === "prepare_sequence_child"
      ) {
        await page.evaluate((payload) => {
          const state = (window as any).__h3StockRuntimeProducer;
          if (!state) throw new Error("stock producer is not installed");
          try {
            const selected = state.selectFixture(
              state.segmentIds,
              state.queues.map((row: any) => row.segmentId),
              payload,
            );
            state.index = selected.index;
            state.segmentId = selected.segmentId;
            state.projectedBytes = null;
          } catch (error) {
            state.selectionFailure = String((error as Error).message);
            throw error;
          }
        }, body.payload);
      }
    }
    // Observe before the real response can launch a successor; preserve all existing routes.
    await route.fallback();
  };
  let installed = false;
  let disposed = false;
  page.on("response", observeResponse);
  await page.route(routePattern, observeBinding);
  const dispose = async (): Promise<void> => {
    if (disposed) return;
    disposed = true;
    const failures: unknown[] = [];
    if (installed) {
      try {
        // Revoke successor permission before removing the test projector: an unprojected
        // successor would otherwise reach the native model graph.
        await page.evaluate(async () => {
          const runtime = window as any;
          const state = runtime.__h3StockRuntimeProducer;
          const parent = state.module.managedProductionSession;
          // Preserve the first actual parent/queue disposition before detach and cancellation
          // replace it. These bounded identities contain no prompt or authority payload.
          runtime.__h3StockRuntimeProducerDiagnostic = {
            snapshot: parent.snapshot(),
            queues: state.queues,
            executed: state.executed,
            socketFailed: state.socketFailed,
            selectionFailure: state.selectionFailure,
          };
          if (
            parent.snapshot().parentSequenceId &&
            !["succeeded", "cancelled"].includes(parent.snapshot().parentState)
          )
            await parent.detach();
        });
      } catch (error) {
        failures.push(error);
      }
      try {
        await page.evaluate(() => {
          const runtime = window as any;
          const state = runtime.__h3StockRuntimeProducer;
          const api = runtime.comfyAPI.api.api;
          state.socket.removeEventListener("message", state.socketListener);
          state.socket.removeEventListener("close", state.socketCloseListener);
          api.removeEventListener("executed", state.listener);
          api.queuePrompt = state.originalQueue;
          if (state.previousProjector === undefined)
            delete runtime.__h3M2508ManagedExecutionProjection;
          else
            runtime.__h3M2508ManagedExecutionProjection =
              state.previousProjector;
          delete runtime.__h3StockRuntimeProducer;
        });
      } catch (error) {
        failures.push(error);
      }
    }
    await page.unroute(routePattern, observeBinding);
    page.off("response", observeResponse);
    await Promise.all(responseReads);
    if (failures.length)
      throw new AggregateError(
        failures,
        "stock producer detach or removal requires caller reconciliation",
      );
  };
  try {
    await page.evaluate(
      async ({
        options,
        inputFilenames,
        projectorSource,
        executionTraceSource,
        selectorSource,
      }) => {
        const runtime = window as any;
        if (!navigator.webdriver || runtime.__h3StockRuntimeProducer)
          throw new Error(
            "stock producer requires an unused webdriver fixture",
          );
        const app = runtime.comfyAPI.app.app;
        const api = runtime.comfyAPI.api.api;
        const module = await import(/* @vite-ignore */ options.bundleUrl);
        const workflow = app.extensionManager.workflow.activeWorkflow;
        if (!workflow) throw new Error("shell workflow authority is absent");
        const parent = module.managedProductionSession;
        const snapshot = parent.snapshot();
        if (
          snapshot.parentSequenceId &&
          !["succeeded", "cancelled"].includes(snapshot.parentState)
        )
          throw new Error("another managed parent is active");
        const project = new Function(`return (${projectorSource})`)();
        const createTrace = new Function(`return (${executionTraceSource})`)();
        const selectFixture = new Function(`return (${selectorSource})`)();
        const socket = api.socket as WebSocket | null;
        if (!socket || socket.readyState !== WebSocket.OPEN)
          throw new Error(
            "stock producer requires an open identity-bearing host socket",
          );
        const originalQueue = api.queuePrompt;
        const previousProjector = runtime.__h3M2508ManagedExecutionProjection;
        const state = {
          module,
          workflow,
          tabs: app.extensionManager.workflow.openWorkflows.length,
          reference: options.ownedReference,
          segmentIds: options.segmentIds,
          selectFixture,
          selectionFailure: null as string | null,
          segmentId: "",
          index: -1,
          lastObservation: null as any,
          projectedBytes: null as string | null,
          sink: "",
          queues: [] as {
            promptId: string;
            outputNodeId: string;
            segmentId: string;
          }[],
          executed:
            [] as ProductionRuntimeProducerResult["sinkExecutions"][number][],
          originalQueue,
          previousProjector,
          listener: null as any,
          socket,
          observedSinks: new Set<string>(),
          executionTrace: createTrace() as ReturnType<
            typeof createStockSinkExecutionTrace
          >,
          socketFailed: false,
          socketListener: null as any,
          socketCloseListener: null as any,
        };
        const canonical = (value: any): string => {
          const visit = (item: any): any =>
            Array.isArray(item)
              ? item.map(visit)
              : item !== null && typeof item === "object"
                ? Object.fromEntries(
                    Object.keys(item)
                      .sort()
                      .map((key) => [key, visit(item[key])]),
                  )
                : item;
          return JSON.stringify(visit(value));
        };
        const verifyWorkflow = (): void => {
          if (
            state.socketFailed ||
            api.socket !== socket ||
            socket.readyState !== WebSocket.OPEN ||
            app.extensionManager.workflow.activeWorkflow !== workflow ||
            app.extensionManager.workflow.openWorkflows.length !== state.tabs
          )
            throw new Error("stock producer workflow ownership changed");
        };
        const adoptOwnership = (): void => {
          verifyWorkflow();
          const observation = state.lastObservation;
          if (!observation) return;
          const graph = app.graph.serialize();
          const ids = new Set(observation.owned_node_ids);
          const owned = graph.nodes.filter((node: any) =>
            ids.has(String(node.id)),
          );
          const shells = new Set(
            owned
              .filter(
                (node: any) =>
                  node.type === "comfyui_h3_context.H3Context.ProductShell",
              )
              .map((node: any) => String(node.id)),
          );
          const edges = graph.links.filter(
            (link: any[]) =>
              shells.has(String(link[1])) && !ids.has(String(link[3])),
          );
          if (owned.length !== ids.size || edges.length !== 1)
            throw new Error("stock producer owned canvas closure is ambiguous");
          const edge = edges[0];
          const anchor = graph.nodes.find(
            (node: any) => String(node.id) === String(edge[3]),
          );
          if (anchor?.inputs?.[edge[4]]?.name !== "prompt")
            throw new Error("stock producer owned prompt anchor is absent");
          state.reference = {
            nodeIds: observation.owned_node_ids,
            linkIds: observation.owned_link_ids,
            anchorNodeId: String(anchor.id),
            authoredWidgetNodeIds: owned
              .filter((node: any) =>
                [
                  "PrimitiveFloat",
                  "comfyui_h3_context.H3Context.Request",
                  "comfyui_h3_context.H3Context.AuditOverride",
                ].includes(node.type),
              )
              .map((node: any) => String(node.id)),
          };
        };
        const projector = (compiled: Compiled): Compiled => {
          if (state.index < 0)
            throw new Error("stock producer child fixture was not selected");
          const projected = project(compiled, {
            inputFilename: inputFilenames[state.index],
            outputPrefix: `${options.runPrefix}_child_${String(state.index + 1).padStart(2, "0")}`,
          });
          const bytes = canonical(projected);
          if (state.projectedBytes !== null && state.projectedBytes !== bytes)
            throw new Error(
              "stock producer repeated prevalidation changed bytes",
            );
          state.projectedBytes = bytes;
          state.sink = Object.entries(projected.output).find(
            ([, node]: any) => node.class_type === "SaveVideo",
          )![0];
          state.observedSinks.add(state.sink);
          return projected;
        };
        const queue = async (
          batch: number,
          compiled: Compiled,
        ): Promise<any> => {
          verifyWorkflow();
          const clone = structuredClone(compiled);
          if (
            state.projectedBytes === null ||
            canonical(compiled) !== state.projectedBytes ||
            canonical(clone) !== state.projectedBytes
          )
            throw new Error(
              "stock producer queue differs from prevalidated bytes",
            );
          const allowed = new Set(
            [
              "Request",
              "Plan",
              "Compiler",
              "Validator",
              "NativeH3Adapter",
              "ProductShell",
              "ReferenceRegistry",
              "AuditOverride",
            ].map((name) => `comfyui_h3_context.H3Context.${name}`),
          );
          for (const name of [
            "PrimitiveFloat",
            "PrimitiveInt",
            "LoadVideo",
            "SaveVideo",
          ])
            allowed.add(name);
          if (
            Object.values(clone.output).some(
              (node) => !allowed.has(node.class_type),
            )
          )
            throw new Error("stock producer queue is not model-free");
          const result = await originalQueue.call(api, batch, clone);
          if (typeof result?.prompt_id !== "string")
            throw new Error("stock producer queue receipt is absent");
          state.queues.push({
            promptId: result.prompt_id,
            outputNodeId: state.sink,
            segmentId: state.segmentId,
          });
          return result;
        };
        state.socketListener = (event: MessageEvent): void => {
          if (typeof event.data !== "string") return;
          if (event.data.length > 1024 * 1024) {
            state.socketFailed = true;
            return;
          }
          try {
            const message = JSON.parse(event.data);
            const node = message?.data?.node;
            // The API listener may advance the child before this listener runs. Retain
            // every already validated sink so the preceding completion is not dropped.
            if (typeof node === "string" && state.observedSinks.has(node))
              state.executionTrace.observe(message, node);
          } catch {
            state.socketFailed = true;
          }
        };
        state.socketCloseListener = (): void => {
          state.socketFailed = true;
        };
        state.listener = (event: CustomEvent): void => {
          const detail = event.detail;
          // Only actual stock SaveVideo events count. The production listener independently
          // validates and captures this same event; never dispatch an event from this helper.
          if (!detail || detail.node !== state.sink) return;
          const output = detail.output;
          const locator = output?.images?.[0];
          if (
            typeof detail.prompt_id !== "string" ||
            Object.keys(output ?? {}).some(
              (key) => !["images", "animated"].includes(key),
            ) ||
            output?.images?.length !== 1 ||
            output?.animated?.length !== 1 ||
            output.animated[0] !== true ||
            !locator ||
            Object.keys(locator).sort().join(",") !==
              "filename,subfolder,type" ||
            typeof locator.filename !== "string" ||
            typeof locator.subfolder !== "string" ||
            locator.type !== "output"
          )
            return;
          state.executed.push({
            promptId: detail.prompt_id,
            outputNodeId: String(detail.node),
            locator: structuredClone(locator),
          });
        };
        const resolveChild = module.createProductionManagedChildResolver({
          app,
          fetchApi: fetch,
          currentOwnedGraphReference: () => state.reference,
        });
        const bindings = {
          resolveChild: async (...args: any[]) => {
            adoptOwnership();
            state.index = args[2];
            state.segmentId = args[0].segmentId;
            if (options.segmentIds[state.index] !== state.segmentId)
              throw new Error(
                "stock producer successor differs from supplied intent",
              );
            state.projectedBytes = null;
            return resolveChild(...args);
          },
        };
        // Keep one live state object shared by projection, queue and the request observer.
        Object.assign(state, { adoptOwnership, bindings });
        runtime.__h3StockRuntimeProducer = state;
        runtime.__h3M2508ManagedExecutionProjection = projector;
        // Pin passive raw-frame observation before any managed Start/queue can execute.
        socket.addEventListener("message", state.socketListener);
        socket.addEventListener("close", state.socketCloseListener);
        api.addEventListener("executed", state.listener);
        api.queuePrompt = queue;
      },
      {
        options,
        inputFilenames,
        projectorSource: projectStockVideoExecution.toString(),
        executionTraceSource: createStockSinkExecutionTrace.toString(),
        selectorSource: selectStockChildFixture.toString(),
      },
    );
    installed = true;
  } catch (error) {
    await dispose().catch(() => undefined);
    throw error;
  }
  const count = segmentIds.length;
  return Object.freeze({
    progress: async () =>
      page.evaluate(() => {
        const state = (window as any).__h3StockRuntimeProducer;
        return {
          queued: state.queues.map((row: any) => ({
            promptId: row.promptId,
            segmentId: row.segmentId,
          })),
          executedPromptIds: state.executed.map((row: any) => row.promptId),
        };
      }),
    collect: async (timeoutMs = options.timeoutMs ?? 180_000) => {
      if (
        !Number.isSafeInteger(timeoutMs) ||
        timeoutMs < 1 ||
        timeoutMs > 900_000
      )
        throw new Error("stock producer timeout is not bounded");
      await page.waitForFunction(
        () => {
          const state = (window as any).__h3StockRuntimeProducer;
          if (state.selectionFailure)
            throw new Error(`stock producer failed: ${state.selectionFailure}`);
          const snapshot = state.module.managedProductionSession.snapshot();
          if (snapshot.failure)
            throw new Error(`stock producer failed: ${snapshot.failure}`);
          if (["failed", "cancelled"].includes(snapshot.parentState))
            throw new Error("stock producer parent did not succeed");
          return snapshot.parentState === "succeeded";
        },
        undefined,
        { timeout: timeoutMs },
      );
      const observed = await page.evaluate(async (workspaceHandle) => {
        const state = (window as any).__h3StockRuntimeProducer;
        await state.module.managedProductionSession.settle();
        state.adoptOwnership();
        if (
          state.socketFailed ||
          (window as any).comfyAPI.api.api.socket !== state.socket ||
          state.socket.readyState !== WebSocket.OPEN
        )
          throw new Error("stock producer socket evidence became incomplete");
        const sinkExecutionProofs = state.executionTrace.prove(state.queues);
        const response = await fetch("/h3-context/v1/production/action", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            schema: "h3.context.production_workbench.action.v1",
            request_id: `stock-read.${crypto.randomUUID()}`,
            action: "read_projection",
            payload: { workspace_handle: workspaceHandle },
          }),
        });
        if (!response.ok)
          throw new Error("stock producer completed workspace read failed");
        return {
          wire: await response.json(),
          parent: state.module.managedProductionSession.snapshot(),
          queues: state.queues,
          executed: state.executed,
          sinkExecutionProofs,
          reference: state.reference,
        };
      }, options.workspaceHandle);
      await Promise.all(responseReads);
      if (observationFailure) throw observationFailure;
      const workspace = decodeProductionWorkbenchProjection(observed.wire);
      if (
        workspace.workspaceHandle !== options.workspaceHandle ||
        workspace.runState !== "succeeded" ||
        observed.queues.length !== count ||
        observed.executed.length !== count ||
        captures.length !== count ||
        new Set(observed.queues.map((row: any) => row.promptId)).size !== count
      )
        throw new Error(
          "stock producer completion or real capture cardinality mismatch",
        );
      for (const [index, row] of observed.queues.entries()) {
        if (
          row.segmentId !== segmentIds[index] ||
          observed.executed.filter(
            (item: any) =>
              item.promptId === row.promptId &&
              item.outputNodeId === row.outputNodeId,
          ).length !== 1 ||
          captures.filter(
            (item) =>
              item.promptId === row.promptId &&
              item.outputNodeId === row.outputNodeId,
          ).length !== 1
        )
          throw new Error(
            "stock producer output does not join its actual child queue",
          );
      }
      return {
        workspace,
        parentSequenceId: observed.parent.parentSequenceId,
        childPromptIds: observed.queues.map((row: any) => row.promptId),
        sinkExecutions: observed.executed,
        sinkExecutionProofs: observed.sinkExecutionProofs,
        captureReceipts: captures,
        ownedReference: observed.reference,
      };
    },
    dispose,
  });
}

/** Execute real stock encodings through the shell-owned parent and capture clients.
 * The caller owns workspace cancellation/release and fixture cleanup, also on failure.
 */
export async function runProductionRuntimeProducer(
  page: Page,
  options: Omit<ProductionRuntimeProducerOptions, "segmentIds" | "start"> &
    Readonly<{ intent: ManagedSerialSequenceStartIntent }>,
): Promise<ProductionRuntimeProducerResult> {
  const { intent } = options;
  const timeoutMs = options.timeoutMs ?? 180_000;
  if (
    options.workspaceHandle !== intent.authorization.workspace_handle ||
    !Number.isSafeInteger(timeoutMs) ||
    timeoutMs < 1 ||
    timeoutMs > 600_000
  )
    throw new Error(
      "stock producer must use the supplied bounded parent intent",
    );
  const handle = await installProductionRuntimeProducer(page, {
    ...options,
    segmentIds: intent.segmentIds,
    start: "direct",
  });
  let failure: unknown;
  try {
    await page.evaluate(async (intent) => {
      const state = (window as any).__h3StockRuntimeProducer;
      await state.module.managedProductionSession.start(intent, state.bindings);
    }, intent);
    return await handle.collect(timeoutMs);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    // Detach before removing the projector; a detach failure joins the primary failure.
    await handle.dispose().catch((cleanupError: unknown) => {
      failure =
        failure === undefined
          ? cleanupError
          : new AggregateError(
              [failure, cleanupError],
              "stock producer failed and detach requires caller reconciliation",
            );
    });
    if (failure !== undefined) throw failure;
  }
}
