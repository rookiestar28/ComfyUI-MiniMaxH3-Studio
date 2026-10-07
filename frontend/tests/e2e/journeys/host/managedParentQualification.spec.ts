import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import type { Page } from "@playwright/test";
import { expect, test } from "../../host/fixture";
import { observeOwnedGraph } from "../../../../src/host/ownedGraphIdentity";
import { MANAGED_SEQUENCE_REATTACH_STORAGE_KEY } from "../../../../src/host/managedSequenceClient";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import { normalizeM2508HostApiPath } from "../../host/m25_08RequestClassification";
import {
  assertCandidateBundleInjection,
  candidateBundleResourceUrl,
  candidateBundle,
  candidateBackendMode,
  candidateInjectionCount,
  hostUrl,
  supportedHostQueueCounts,
  openH3AppModeTab,
  m23TerminalWaitingArtifactLocator,
  waitForH3Registration,
} from "../../host/environment";

async function qualifyParentChildren(
  page: Page,
  prepared: { intent?: unknown; workspace?: Record<string, any> },
  queues: () => number,
): Promise<void> {
  if (!hostUrl) throw new Error("supplied host unavailable");
  const browserTabs = page.context().pages().length;
  const ownedSnapshots = new Map<number, any>();
  const bindings: Record<string, any>[] = [];
  const submissions: Record<string, any>[] = [];
  const historyReads: string[] = [];
  const managedActions: string[] = [];
  const refusalReads: Promise<void>[] = [];
  page.on("response", (response) => {
    if (
      response.status() < 400 ||
      normalizeM2508HostApiPath(new URL(response.url()).pathname) !==
        "/h3-context/v1/managed-sequences"
    )
      return;
    refusalReads.push(
      response
        .json()
        .then((body) => {
          console.info("m26_04_managed_refusal", {
            status: response.status(),
            code:
              typeof body.error === "string" &&
              /^[a-z_]{1,80}$/.test(body.error)
                ? body.error
                : "unclassified",
          });
        })
        .catch(() => {
          console.info("m26_04_managed_refusal", {
            status: response.status(),
            code: "body_unavailable",
          });
        }),
    );
  });
  page.on("request", (request) => {
    // CRITICAL: ComfyUI's public API client may prefix /api; count both route forms.
    // Exact unprefixed matching would report zero effects after real child submission.
    const pathname = normalizeM2508HostApiPath(new URL(request.url()).pathname);
    if (request.method() === "GET" && pathname.startsWith("/history/"))
      historyReads.push(pathname);
    if (
      request.method() !== "POST" ||
      pathname !== "/h3-context/v1/managed-sequences"
    )
      return;
    const body = request.postDataJSON();
    managedActions.push(body.action);
    if (body.action === "bind_prepared_child") bindings.push(body.payload);
    if (body.action === "record_submission") submissions.push(body.payload);
  });
  await page.evaluate(async (bundleUrl) => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const api = runtime.comfyAPI.api.api;
    const module = await import(/* @vite-ignore */ bundleUrl);
    await app.loadGraphData({
      version: 0.4,
      nodes: [],
      links: [],
      groups: [],
      config: {},
      extra: {},
    });
    const workflow = app.extensionManager.workflow.activeWorkflow;
    if (!workflow) throw new Error("fresh workflow authority unavailable");
    const state = {
      module,
      workflow,
      tabs: app.extensionManager.workflow.openWorkflows.length,
      reference: {
        nodeIds: [],
        linkIds: [],
        anchorNodeId: "unbound",
        authoredWidgetNodeIds: [],
      },
      queueReceipts: [] as Record<string, unknown>[],
      projections: new Map<string, string>(),
      sink: "",
      bindings: null as any,
      projectedWorkflow: null as any,
    };
    runtime.__h3ParentQualification = state;
    runtime.__h3ProjectionTrace = [];
    const canonical = (value: any): string => {
      if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
      if (value !== null && typeof value === "object")
        return `{${Object.keys(value)
          .sort()
          .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`)
          .join(",")}}`;
      return JSON.stringify(value);
    };
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
    // CRITICAL: the accepted model-free closure includes linked duration/size primitives.
    // Omitting them rejects normal materialized prompts before the first child queue.
    allowed.add("PrimitiveFloat");
    allowed.add("PrimitiveInt");
    const originalQueue = api.queuePrompt.bind(api);
    // CRITICAL: project at the accepted pre-validation seam. The final queue wrapper
    // only observes and delegates the identical clone; it never removes native nodes.
    runtime.__h3M2508ManagedExecutionProjection = (compiled: any) => {
      state.projectedWorkflow = structuredClone(compiled.workflow);
      const output = compiled.output;
      const rows = Object.entries(output) as [string, any][];
      const shells = rows.filter(
        ([, node]) =>
          node.class_type === "comfyui_h3_context.H3Context.ProductShell",
      );
      const sinks = rows.filter(([, node]) => node.class_type === "SaveVideo");
      if (shells.length !== 1 || sinks.length !== 1)
        throw new Error("complete generation envelope unavailable");
      state.sink = sinks[0]![0];
      const closure = new Set<string>();
      const pending = [shells[0]![0]];
      while (pending.length) {
        const id = pending.pop()!;
        if (closure.has(id)) continue;
        const node = output[id];
        if (!node || !allowed.has(node.class_type) || closure.size >= 128)
          throw new Error("non-model-free execution closure");
        closure.add(id);
        for (const value of Object.values(node.inputs ?? {})) {
          if (
            Array.isArray(value) &&
            value.length === 2 &&
            Object.hasOwn(output, String(value[0]))
          )
            pending.push(String(value[0]));
        }
      }
      const projected = {
        ...structuredClone(compiled),
        output: Object.fromEntries(rows.filter(([id]) => closure.has(id))),
      };
      const key = shells[0]![0];
      const identity = canonical(projected);
      const old = state.projections.get(key);
      if (old !== undefined && old !== identity)
        throw new Error("preflight/final projection drift");
      state.projections.set(key, identity);
      return projected;
    };
    api.queuePrompt = async (batch: unknown, compiled: any) => {
      const clone = structuredClone(compiled);
      const identity = canonical(clone);
      if (
        identity !== canonical(compiled) ||
        ![...state.projections.values()].includes(identity)
      )
        throw new Error("queue identity drift");
      if (
        Object.values(clone.output).some(
          (node: any) => !allowed.has(node.class_type),
        )
      )
        throw new Error("native execution refused");
      const result = await originalQueue(batch, clone);
      state.queueReceipts.push({
        promptId: result.prompt_id,
        unchanged: true,
        modelFree: true,
        sink: state.sink,
      });
      return result;
    };
    const resolveChild = module.createProductionManagedChildResolver({
      app,
      fetchApi: fetch,
      currentOwnedGraphReference: () => state.reference,
    });
    state.bindings = {
      resolveChild: async (...args: unknown[]) => {
        state.projections.clear();
        return resolveChild(...args);
      },
    };
  }, candidateBundleResourceUrl(hostUrl));
  const snapshot = () =>
    page.evaluate(() =>
      (
        window as any
      ).__h3ParentQualification.module.managedProductionSession.snapshot(),
    );
  const waitTerminal = async (promptId: string) => {
    await expect
      .poll(async () => {
        const response = await page.request.get(
          new URL(`/history/${encodeURIComponent(promptId)}`, hostUrl).href,
        );
        return (await response.json())[promptId]?.status?.status_str;
      })
      .toBe("success");
    await page.evaluate(async () =>
      (
        window as any
      ).__h3ParentQualification.module.managedProductionSession.settle(),
    );
  };
  const deliver = async (promptId: string, sink: string) => {
    await page.evaluate(
      ({ promptId, sink, locator }) => {
        (window as any).comfyAPI.api.api.dispatchEvent(
          new CustomEvent("executed", {
            detail: {
              prompt_id: promptId,
              node: sink,
              output: { images: [locator], animated: [true] },
            },
          }),
        );
      },
      { promptId, sink, locator: m23TerminalWaitingArtifactLocator! },
    );
    await page.evaluate(async () =>
      (
        window as any
      ).__h3ParentQualification.module.managedProductionSession.settle(),
    );
  };
  const adoptObservedOwnership = async (index: number) => {
    const binding = bindings[index];
    if (!binding) throw new Error("real prepared binding missing");
    const observed = await page.evaluate((observation) => {
      const state = (window as any).__h3ParentQualification;
      const app = (window as any).comfyAPI.app.app;
      const graph = app.graph.serialize();
      const owned = new Set(observation.owned_node_ids);
      const ownedLinks = new Set(observation.owned_link_ids);
      const shells = new Set(
        graph.nodes
          .filter(
            (node: any) =>
              owned.has(String(node.id)) &&
              node.type === "comfyui_h3_context.H3Context.ProductShell",
          )
          .map((node: any) => String(node.id)),
      );
      const promptEdges = graph.links.filter(
        (link: any[]) =>
          ownedLinks.has(String(link[0])) &&
          shells.has(String(link[1])) &&
          !owned.has(String(link[3])),
      );
      if (promptEdges.length !== 1)
        throw new Error("owned canvas prompt edge unavailable");
      const anchor = graph.nodes.find(
        (node: any) => String(node.id) === String(promptEdges[0][3]),
      );
      if (anchor?.inputs?.[promptEdges[0][4]]?.name !== "prompt")
        throw new Error("owned canvas prompt binding unavailable");
      const authored = graph.nodes
        .filter(
          (node: any) =>
            owned.has(String(node.id)) &&
            [
              "PrimitiveFloat",
              "comfyui_h3_context.H3Context.Request",
              "comfyui_h3_context.H3Context.AuditOverride",
            ].includes(node.type),
        )
        .map((node: any) => String(node.id));
      state.reference = {
        nodeIds: observation.owned_node_ids,
        linkIds: observation.owned_link_ids,
        // CRITICAL: native_anchor_node_id is a compiled execution ID, which can be nested.
        // The owned canvas fingerprint uses the root prompt-edge target written by the splice.
        anchorNodeId: String(anchor.id),
        authoredWidgetNodeIds: authored,
      };
      return {
        graph,
        projectedWorkflow: state.projectedWorkflow,
        reference: state.reference,
        workflowSame:
          app.extensionManager.workflow.activeWorkflow === state.workflow,
        tabsSame:
          app.extensionManager.workflow.openWorkflows.length === state.tabs,
      };
    }, binding.observation);
    expect(observed.workflowSame).toBe(true);
    expect(observed.tabsSame).toBe(true);
    const fingerprint = observeOwnedGraph(
      observed.graph,
      observed.reference,
    ).fingerprint;
    if (fingerprint !== binding.owned_projection_fingerprint) {
      console.info(
        "m26_04_owned_observation_diagnostic",
        JSON.stringify({
          anchorFound: observed.graph.nodes.some(
            (node: any) => String(node.id) === observed.reference.anchorNodeId,
          ),
          authoredCount: observed.reference.authoredWidgetNodeIds.length,
          projectedMatchesBinding:
            observeOwnedGraph(observed.projectedWorkflow, observed.reference)
              .fingerprint === binding.owned_projection_fingerprint,
          surroundings: diffGraphSurroundings({
            beforeValue: observed.projectedWorkflow,
            afterValue: observed.graph,
            reference: {
              ownedNodeIds: observed.reference.nodeIds,
              ownedLinkIds: observed.reference.linkIds,
              anchorNodeId: observed.reference.anchorNodeId,
              authoredWidgetNodeIds: observed.reference.authoredWidgetNodeIds,
              ownedProjectionEqual: false,
            },
          }),
        }),
      );
    }
    expect(fingerprint).toBe(binding.owned_projection_fingerprint);
    const previous = ownedSnapshots.get(index);
    if (previous !== undefined) {
      const surroundings = diffGraphSurroundings({
        beforeValue: previous.graph,
        afterValue: observed.graph,
        reference: {
          ownedNodeIds: observed.reference.nodeIds,
          ownedLinkIds: observed.reference.linkIds,
          anchorNodeId: observed.reference.anchorNodeId,
          authoredWidgetNodeIds: observed.reference.authoredWidgetNodeIds,
          ownedProjectionEqual: true,
        },
      });
      expect(surroundings.counts.owned).toBe(0);
      console.log("M26_04_SURROUNDINGS=" + JSON.stringify(surroundings));
    }
    ownedSnapshots.set(index, observed);
    expect(page.context().pages()).toHaveLength(browserTabs);
    return {
      activeWorkflowFingerprint: binding.active_workflow_fingerprint,
      ownedProjectionFingerprint: fingerprint,
    };
  };
  try {
    expect(queues()).toBe(0);
    await page.evaluate(async (intent) => {
      const state = (window as any).__h3ParentQualification;
      await state.module.managedProductionSession.start(intent, state.bindings);
    }, prepared.intent);
    const started = await snapshot();
    console.info("m26_04_parent_started", {
      parentState: started.parentState,
      failure: started.failure,
      attached: started.attached,
      queueCalls: queues(),
      bindings: bindings.length,
      submissions: submissions.length,
      managedActions,
    });
    await expect.poll(() => submissions.length).toBe(1);
    const first = submissions[0]!;
    expect(queues()).toBe(1);
    await waitTerminal(first.queue_prompt_id);
    expect((await snapshot()).activeQueuePromptId).toBe(first.queue_prompt_id);
    expect(queues()).toBe(1);
    const identity = await adoptObservedOwnership(0);
    const firstSink = await page.evaluate(
      () => (window as any).__h3ParentQualification.queueReceipts[0].sink,
    );
    await deliver("foreign.qualification.prompt", firstSink);
    expect(queues()).toBe(1);
    await page.evaluate(async () =>
      (
        window as any
      ).__h3ParentQualification.module.managedProductionSession.detach(),
    );
    expect((await snapshot()).attached).toBe(false);
    // Negative recovery inputs are derived only from this run's real pointer and
    // restored byte-for-byte; they never create a positive receipt or leave the page.
    for (const scenario of ["lost", "expired", "foreign"] as const) {
      const refused = await page.evaluate(
        async ({ scenario, key }) => {
          const state = (window as any).__h3ParentQualification;
          const retained = localStorage.getItem(key);
          if (!retained) throw new Error("real owned recovery pointer missing");
          try {
            if (scenario === "lost") localStorage.removeItem(key);
            else {
              const pointer = JSON.parse(retained);
              if (scenario === "expired") pointer.expires_at_epoch_ms = 0;
              else {
                const fingerprint =
                  pointer.read_authority_fingerprint as string;
                pointer.read_authority_fingerprint =
                  fingerprint.slice(0, -1) +
                  (fingerprint.endsWith("0") ? "1" : "0");
              }
              localStorage.setItem(key, JSON.stringify(pointer));
            }
            try {
              const result =
                await state.module.managedProductionSession.reattach(
                  state.bindings,
                );
              return result.disposition === "recovery_unavailable";
            } catch (error: any) {
              return (
                scenario === "foreign" &&
                error.name === "ManagedSequenceClientError" &&
                ["invalid_response", "managed_sequence_read_rejected"].includes(
                  error.code,
                )
              );
            }
          } finally {
            localStorage.setItem(key, retained);
          }
        },
        { scenario, key: MANAGED_SEQUENCE_REATTACH_STORAGE_KEY },
      );
      expect(refused).toBe(true);
      expect(queues()).toBe(1);
    }
    const historyCount = historyReads.length;
    const beforeArtifact = await page.evaluate(async () => {
      const state = (window as any).__h3ParentQualification;
      return state.module.managedProductionSession.reattach(state.bindings);
    });
    expect(beforeArtifact.disposition).toBe("artifact_authority_unavailable");
    expect(beforeArtifact.activePromptId).toBe(first.queue_prompt_id);
    expect(historyReads.slice(historyCount)).toEqual([
      `/history/${encodeURIComponent(first.queue_prompt_id)}`,
    ]);
    expect(queues()).toBe(1);
    // This explicit fixture event is artifact-delivery evidence only. Bytes pass
    // through the unchanged production capture/store; no model generation is claimed.
    await deliver(first.queue_prompt_id, firstSink);
    await expect
      .poll(async () => (await snapshot()).activeSegmentId)
      .toBeNull();
    expect((await snapshot()).parentState).toBe("paused_client_absent");
    expect(queues()).toBe(1);
    const recovered = await page.evaluate(async () => {
      const state = (window as any).__h3ParentQualification;
      return state.module.managedProductionSession.reattach(state.bindings);
    });
    expect(recovered.disposition).toBe("resumable");
    expect(queues()).toBe(1);
    await adoptObservedOwnership(0);
    await page.evaluate(
      async (identity) =>
        (
          window as any
        ).__h3ParentQualification.module.managedProductionSession.resume(
          identity,
        ),
      identity,
    );
    await expect.poll(() => submissions.length).toBe(2);
    const second = submissions[1]!;
    expect(second.parent_sequence_id).toBe(first.parent_sequence_id);
    expect(second.segment_id).not.toBe(first.segment_id);
    expect(second.queue_prompt_id).not.toBe(first.queue_prompt_id);
    expect(queues()).toBe(2);
    await waitTerminal(second.queue_prompt_id);
    await adoptObservedOwnership(1);
    await deliver(first.queue_prompt_id, firstSink);
    expect((await snapshot()).activeQueuePromptId).toBe(second.queue_prompt_id);
    const secondSink = await page.evaluate(
      () => (window as any).__h3ParentQualification.queueReceipts[1].sink,
    );
    await deliver(second.queue_prompt_id, secondSink);
    const delivered = await snapshot();
    const projectionStages = await page.evaluate(() =>
      ((window as any).__h3ProjectionTrace ?? []).map(
        (entry: any) => entry.stage,
      ),
    );
    console.info("m26_04_parent_delivered", {
      parentState: delivered.parentState,
      attached: delivered.attached,
      failure: delivered.failure,
      managedActions,
      projectionStages,
    });
    await expect
      .poll(async () => (await snapshot()).parentState)
      .toBe("succeeded");
    await adoptObservedOwnership(1);
    expect((await snapshot()).failure).toBeNull();
    expect(queues()).toBe(2);
    // The production submission action atomically commits the owned canvas write.
    expect(submissions).toHaveLength(2);
    for (const [index, write] of submissions.entries()) {
      expect(write.written_owned_projection_fingerprint).toBe(
        bindings[index]!.owned_projection_fingerprint,
      );
      expect(write.active_workflow_fingerprint).toBe(
        identity.activeWorkflowFingerprint,
      );
    }
    expect(second.previous_owned_projection_fingerprint).toBe(
      first.written_owned_projection_fingerprint,
    );
    console.log(
      "M26_04_PARENT_B1=" +
        JSON.stringify({
          children: 2,
          ownedCanvasWriteCommits: submissions.length,
          terminalAloneHeld: true,
          foreignAndStaleArtifactHeld: true,
          detachedHeld: true,
          lostExpiredForeignPointerHeld: true,
          exactPromptHistoryWithoutArtifactHeld: true,
          reattachQueued: 0,
          explicitResumeQueued: 1,
          sameParent: true,
          succeeded: true,
        }),
    );
  } finally {
    await Promise.all(refusalReads);
    await page.evaluate(async (workspace) => {
      const state = (window as any).__h3ParentQualification;
      const parent = state.module.managedProductionSession;
      if (
        parent.snapshot().parentSequenceId &&
        !["succeeded", "cancelled"].includes(parent.snapshot().parentState)
      )
        await parent.cancel();
      const send = async (action: string, payload: Record<string, unknown>) => {
        const response = await fetch("/h3-context/v1/production/action", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            schema: "h3.context.production_workbench.action.v1",
            request_id: `cleanup.${crypto.randomUUID()}`,
            action,
            payload,
          }),
        });
        if (!response.ok) throw new Error("owned workspace cleanup refused");
        return response.status === 204 ? null : response.json();
      };
      const current = await send("read_projection", {
        workspace_handle: workspace!.workspace_handle,
      });
      await send("release_workspace", {
        workspace_handle: current.workspace_handle,
        expected_workspace_revision: current.workspace_revision,
        expected_workspace_fingerprint: current.workspace_fingerprint,
      });
    }, prepared.workspace);
  }
}

for (const runChildren of [false, true]) {
  test(
    runChildren
      ? "real parent waits for owned artifacts and resumes B1 only explicitly"
      : "real readiness producer admits and releases an exact parent without a child queue",
    async ({ page, context }) => {
      test.setTimeout(120_000);
      if (
        !hostUrl ||
        candidateBundle === null ||
        candidateBackendMode !== "exact"
      )
        throw new Error("an exact supplied-host candidate is required");
      if (runChildren && m23TerminalWaitingArtifactLocator === null)
        throw new Error(
          "the authorized content-free artifact fixture is required",
        );
      expect(await supportedHostQueueCounts(page)).toEqual({
        running: 0,
        pending: 0,
      });
      let queueCalls = 0;
      page.on("request", (request) => {
        if (
          request.method() === "POST" &&
          normalizeM2508HostApiPath(new URL(request.url()).pathname) ===
            "/prompt"
        )
          queueCalls++;
      });
      const injections = candidateInjectionCount(context);
      await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
      await waitForH3Registration(page);
      await assertCandidateBundleInjection(page, context, injections);
      if (runChildren)
        await openH3AppModeTab(page, "managed-parent-qualification");
      const fixture = JSON.parse(
        await readFile(
          resolve(process.cwd(), "../workflows/m15_03_product_shell_base.json"),
          "utf8",
        ),
      );
      const bootstrap = structuredClone(fixture.prompt);
      // This separate Context bootstrap never includes a native generation node.
      delete bootstrap["7"];
      bootstrap["1"].inputs.duration_seconds = 10;
      bootstrap["1"].inputs.user_intent =
        `A blue sphere turns slowly. Context ${Date.now()}.`;
      const setup = await page.evaluate(async (prompt) => {
        const response = await fetch("/prompt", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ prompt }),
        });
        if (!response.ok) throw new Error("Context bootstrap refused");
        return String((await response.json()).prompt_id);
      }, bootstrap);
      let source: Record<string, unknown> | undefined;
      await expect
        .poll(async () => {
          const response = await page.request.get(
            new URL(`/history/${encodeURIComponent(setup)}`, hostUrl).href,
          );
          source = (await response.json())[setup]?.outputs?.["6"]
            ?.sidebar_workspace?.[0];
          return source !== undefined;
        })
        .toBe(true);
      await expect
        .poll(() => supportedHostQueueCounts(page))
        .toEqual({ running: 0, pending: 0 });
      const before = queueCalls;
      const receipt = await page.evaluate(
        async ({ source, bundleUrl, runChildren }) => {
          const module = await import(/* @vite-ignore */ bundleUrl);
          const planning = module.createProductionPlanningClient({
            fetchApi: fetch,
          });
          const readiness = module.createManagedQualificationClient({
            fetchApi: fetch,
          });
          const prefix = `parent.${crypto.randomUUID()}`;
          async function post(
            route: string,
            schema: string,
            action: string,
            payload: Record<string, unknown>,
          ) {
            const response = await fetch(route, {
              method: "POST",
              credentials: "same-origin",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({
                schema,
                request_id: `${prefix}.${crypto.randomUUID()}`,
                action,
                payload,
              }),
            });
            if (!response.ok)
              throw new Error(
                `Owned action refused: ${action}/${response.status}`,
              );
            return response.status === 204 ? null : response.json();
          }
          const production = (
            action: string,
            payload: Record<string, unknown>,
          ) =>
            post(
              "/h3-context/v1/production/action",
              "h3.context.production_workbench.action.v1",
              action,
              payload,
            );
          const parent = (action: string, payload: Record<string, unknown>) =>
            post(
              "/h3-context/v1/managed-sequences",
              "h3.context.managed_sequence_action.v1",
              action,
              payload,
            );
          const workspace = await production("create_workspace_from_context", {
            context_workspace_handle: source.workspace_id,
          });
          let active: Record<string, any> | null = null;
          let retained = false;
          const authority = () => ({
            parent_sequence_id: active!.parent_sequence_id,
            expected_revision: active!.revision,
            authorization_fingerprint: active!.authorization_fingerprint,
          });
          try {
            const prepared = await planning.send(
              `${prefix}.prepare`,
              "prepare_context",
              {
                workspace_handle: workspace.workspace_handle,
                expected_workspace_revision: workspace.workspace_revision,
                expected_workspace_fingerprint: workspace.workspace_fingerprint,
                context_workspace_handle: source.workspace_id,
                expected_report_revision: source.report_revision,
                expected_report_fingerprint: source.report_fingerprint,
                expected_planning_revision: 0,
                target_seconds: 20,
                policy: "fixed_10",
              },
            );
            const admitted = await planning.send(
              `${prefix}.admit`,
              "admit_storyboard",
              {
                ...module.planningSelectors(prepared),
                source_kind: "canonical_optimized_prompt",
                user_reviewed: false,
                typed_rows: [],
              },
            );
            const proposed = await planning.send(
              `${prefix}.propose`,
              "propose",
              {
                ...module.planningSelectors(admitted),
                admission_id: admitted.admission_id,
              },
            );
            const imported = await planning.send(
              `${prefix}.import`,
              "import_plan",
              {
                ...module.planningSelectors(proposed),
                proposal_id: proposed.proposal.proposal_id,
              },
            );
            const selection = {
              workspace_handle: workspace.workspace_handle,
              expected_workspace_revision: imported.workspace_revision,
              expected_workspace_fingerprint: imported.workspace_fingerprint,
              expected_plan_fingerprint: imported.plan_fingerprint,
            };
            const ready = await readiness.send(
              `${prefix}.readiness`,
              "prepare_managed_readiness",
              selection,
            );
            if (ready.status !== "ready")
              throw new Error(`Readiness held: ${ready.reason}`);
            const intent = module.buildQualifiedManagedStartIntent(
              ready,
              selection,
              imported,
              proposed.proposal.fingerprint,
            );
            if (runChildren) {
              retained = true;
              return { workspace, intent };
            }
            active = await parent("authorize_sequence", intent.authorization);
            active = await parent("start_sequence", {
              ...authority(),
              qualification_fingerprint: intent.qualificationFingerprint,
            });
            const state = active!.state;
            active = await parent("cancel_sequence", authority());
            return {
              state,
              cancelled: active!.state,
              segments: intent.segmentIds.length,
              exactPlan:
                ready.qualification.production_plan_fingerprint ===
                imported.plan_fingerprint,
            };
          } finally {
            if (active && active.state !== "cancelled")
              await parent("cancel_sequence", authority());
            if (!retained) {
              const current = await production("read_projection", {
                workspace_handle: workspace.workspace_handle,
              });
              await production("release_workspace", {
                workspace_handle: current.workspace_handle,
                expected_workspace_revision: current.workspace_revision,
                expected_workspace_fingerprint: current.workspace_fingerprint,
              });
            }
          }
        },
        {
          source: source!,
          bundleUrl: candidateBundleResourceUrl(hostUrl),
          runChildren,
        },
      );
      if (runChildren) {
        if (!("intent" in receipt))
          throw new Error("parent intent unavailable");
        await qualifyParentChildren(page, receipt, () => queueCalls - before);
        return;
      }
      expect(receipt).toEqual({
        state: "active",
        cancelled: "cancelled",
        segments: 2,
        exactPlan: true,
      });
      expect(queueCalls - before).toBe(0);
      expect(before).toBe(1);
      console.log(`M26_04_PARENT_PRECONDITION=${JSON.stringify(receipt)}`);
    },
  );
}
