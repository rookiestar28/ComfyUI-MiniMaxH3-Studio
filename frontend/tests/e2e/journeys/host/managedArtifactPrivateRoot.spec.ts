import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import type { Response } from "@playwright/test";
import {
  decodeProductionWorkbenchProjection,
  encodeProductionAction,
} from "../../../../src/contracts/productionWorkbenchCodec";
import { SEQUENCE_COORDINATOR_ACTION_SCHEMA } from "../../../../src/host/sequenceCoordinator";
import { expect, test } from "../../host/fixture";
import {
  assertCandidateBundleInjection,
  candidateBundleResourceUrl,
  candidateInjectionCount,
  hostUrl,
  instrumentHostGraphLoads,
  openH3AppModeTab,
  readVisibleGraph,
  repositoryRoot,
  setSupportedH3Language,
  supportedHostQueueCounts,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "../../host/environment";
import {
  ensureM2508CapturedWorkflowAuthority,
  materializeM2508VisiblePromptInCapturedWorkflow,
} from "../../host/m25_08Bootstrap";
import { normalizeM2508HostApiPath } from "../../host/m25_08RequestClassification";
import {
  installProductionRuntimeProducer,
  type ProductionRuntimeProducerHandle,
} from "../../host/productionRuntimeProducer";
import { ownedPath } from "../../host/productionRuntimeRow";

type Fixture = {
  schema: "managed-private-root-fixture-v1";
  phase: "before" | "after";
  inputFilename: string;
  runPrefix: string;
  evidenceRelative: string;
  operationalRelative: string;
};
const fixturePath = process.env.H3_CONTEXT_MANAGED_PRIVATE_ROOT_FIXTURE;
const PRODUCTION = "/h3-context/v1/production/action";
const COORDINATOR = "/h3-context/v1/generation/coordinator";

test("managed copies use private storage across an explicit owned restart", async ({
  page,
  context,
}) => {
  test.skip(
    !hostUrl || !fixturePath,
    "An explicitly prepared supplied-host fixture is required",
  );
  test.setTimeout(240_000);
  const fixture = JSON.parse(
    await readFile(ownedPath(fixturePath!), "utf8"),
  ) as Fixture;
  if (
    Object.keys(fixture).sort().join() !==
      "evidenceRelative,inputFilename,operationalRelative,phase,runPrefix,schema" ||
    fixture.schema !== "managed-private-root-fixture-v1" ||
    !["before", "after"].includes(fixture.phase) ||
    !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,190}\.mp4$/.test(fixture.inputFilename) ||
    !/^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$/.test(fixture.runPrefix)
  )
    throw new Error("Invalid bounded private-storage host fixture");
  const evidence = ownedPath(fixture.evidenceRelative);
  const operational = ownedPath(fixture.operationalRelative);
  const bundleUrl = candidateBundleResourceUrl(hostUrl!);
  let serial = 0;
  const requestId = () => `private-scope.${fixture.phase}.${++serial}`;
  let workspaceHandle: string | undefined;
  let producer: ProductionRuntimeProducerHandle | undefined;
  let succeeded = false;
  let promptPosts = 0;
  let primaryFailure: unknown;
  let stage = "admission";
  const oldAuthorityRefusals: number[] = [];
  const actionStatuses: {
    action: string;
    status: number;
    code: string | null;
  }[] = [];
  const responseTasks: Promise<void>[] = [];
  const responseErrors: unknown[] = [];
  const observe = (response: Response) => {
    const path = normalizeM2508HostApiPath(new URL(response.url()).pathname);
    if (
      [PRODUCTION, COORDINATOR, "/h3-context/v1/managed-sequences"].includes(
        path,
      ) &&
      response.request().method() === "POST"
    ) {
      const action = response.request().postDataJSON()?.action;
      if (typeof action === "string" && actionStatuses.length < 128) {
        const observed = {
          action,
          status: response.status(),
          code: null as string | null,
        };
        actionStatuses.push(observed);
        if (response.status() >= 400)
          responseTasks.push(
            response
              .text()
              .then((text) => {
                // Host boundary refusals may intentionally have an empty body.
                // Retain their status without manufacturing a JSON cleanup failure.
                if (text === "") return;
                const wire = JSON.parse(text);
                const code = wire?.code ?? wire?.error?.code ?? wire?.error;
                if (
                  typeof code === "string" &&
                  /^[a-z][a-z0-9_]{0,127}$/.test(code)
                )
                  observed.code = code;
              })
              .catch((error: unknown) => {
                responseErrors.push(error);
              }),
          );
      }
    }
    if (
      // CRITICAL: the official host facade prefixes API routes. Comparing the raw
      // pathname loses a real create response and its required cleanup authority.
      normalizeM2508HostApiPath(new URL(response.url()).pathname) !==
        PRODUCTION ||
      response.request().method() !== "POST"
    )
      return;
    if (
      response.request().postDataJSON()?.action ===
        "create_workspace_from_context" &&
      response.status() === 201
    )
      responseTasks.push(
        response
          .json()
          .then((wire) => {
            workspaceHandle =
              decodeProductionWorkbenchProjection(wire).workspaceHandle;
          })
          .catch((error: unknown) => {
            responseErrors.push(error);
          }),
      );
  };
  const post = async (
    _page: typeof page,
    route: string,
    body: Record<string, unknown>,
  ) =>
    page.evaluate(
      async ({ route, body }) => {
        const runtime = window as unknown as {
          comfyAPI: {
            api: {
              api: {
                fetchApi(
                  path: string,
                  init: RequestInit,
                ): Promise<globalThis.Response>;
              };
            };
          };
        };
        const response = await runtime.comfyAPI.api.api.fetchApi(route, {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        const text = await response.text();
        return {
          status: response.status,
          body: text === "" ? null : JSON.parse(text),
        };
      },
      { route, body },
    );
  page.on("response", observe);
  // CRITICAL: this row allows only the declared model-free bootstrap and two stock
  // encodings. A broken projector must fail before any native model can be queued.
  await page.route("**/prompt", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const wire = route.request().postDataJSON();
    const classes = new Set(
      [
        "Request",
        "Plan",
        "Compiler",
        "Validator",
        "NativeH3Adapter",
        "ProductShell",
        "Preview",
        "ReferenceRegistry",
        "AuditOverride",
      ].map((name) => `comfyui_h3_context.H3Context.${name}`),
    );
    for (const name of [
      "LoadVideo",
      "SaveVideo",
      "PrimitiveFloat",
      "PrimitiveInt",
    ])
      classes.add(name);
    const nodes = Object.values(wire.prompt ?? {}) as {
      class_type: string;
      inputs: Record<string, unknown>;
    }[];
    if (
      ++promptPosts > 3 ||
      nodes.length === 0 ||
      nodes.length > 128 ||
      nodes.some((node) => !classes.has(node.class_type))
    ) {
      await route.abort();
      throw new Error(
        "Private-storage journey exceeded its closed queue budget",
      );
    }
    await route.fallback();
  });
  const readProjection = async () => {
    const response = await post(
      page,
      PRODUCTION,
      encodeProductionAction(requestId(), "read_projection", {
        workspaceHandle: workspaceHandle!,
      }),
    );
    expect(response.status).toBe(200);
    return decodeProductionWorkbenchProjection(response.body);
  };
  try {
    expect(await supportedHostQueueCounts(page)).toEqual({
      running: 0,
      pending: 0,
    });
    const injection = candidateInjectionCount(context);
    await page.goto(hostUrl!, { waitUntil: "domcontentloaded" });
    await waitForH3Registration(page);
    await assertCandidateBundleInjection(page, context, injection);
    await setSupportedH3Language(page, "en");
    await instrumentHostGraphLoads(page);
    stage = "workflow_lifecycle";
    const shell = await openH3AppModeTab(page, "h3-managed-private-scope");
    // CRITICAL: registration precedes workflow activation. Settle the real tracked
    // lifecycle before capturing authority; an in-flight tracker is not an owned graph.
    await waitForHostGraphSettled(page);
    await page.evaluate(ensureM2508CapturedWorkflowAuthority);
    if (fixture.phase === "after") {
      const prior = JSON.parse(await readFile(operational, "utf8")) as {
        workspaceHandle: string;
        runHandles: string[];
      };
      const gone = await post(
        page,
        PRODUCTION,
        encodeProductionAction(requestId(), "read_projection", {
          workspaceHandle: prior.workspaceHandle,
        }),
      );
      oldAuthorityRefusals.push(gone.status);
      expect([404, 410]).toContain(gone.status);
      for (const handle of prior.runHandles) {
        const stale = await post(page, COORDINATOR, {
          schema: SEQUENCE_COORDINATOR_ACTION_SCHEMA,
          request_id: requestId(),
          action: "read_sequence",
          payload: { run_handle: handle },
        });
        oldAuthorityRefusals.push(stale.status);
        expect([404, 410]).toContain(stale.status);
      }
      expect(promptPosts).toBe(0);
    }
    const source = JSON.parse(
      await readFile(
        resolve(repositoryRoot, "workflows/m15_03_product_shell_base.json"),
        "utf8",
      ),
    );
    const bootstrap = structuredClone(source.prompt);
    delete bootstrap["7"];
    bootstrap["1"].inputs.duration_seconds = 15;
    bootstrap["1"].inputs.user_intent =
      `Synthetic color video. ${fixture.runPrefix}.`;
    await page.evaluate(materializeM2508VisiblePromptInCapturedWorkflow, {
      ...bootstrap,
      "7": source.prompt["7"],
    });
    await page.evaluate(ensureM2508CapturedWorkflowAuthority);
    await waitForHostGraphSettled(page);
    const promptId = await page.evaluate(async (prompt) => {
      const runtime = window as any;
      const response = await fetch("/prompt", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          prompt,
          client_id: runtime.comfyAPI.api.api.clientId,
        }),
      });
      if (!response.ok)
        throw new Error(
          `Model-free context bootstrap refused: ${response.status}`,
        );
      return String((await response.json()).prompt_id);
    }, bootstrap);
    stage = "bootstrap_projection";
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            new URL(`/history/${encodeURIComponent(promptId)}`, hostUrl!).href,
          );
          return (
            (await response.json())[promptId]?.outputs?.["6"]
              ?.sidebar_workspace?.[0] !== undefined
          );
        },
        { timeout: 60_000 },
      )
      .toBe(true);
    await expect
      .poll(() => supportedHostQueueCounts(page))
      .toEqual({ running: 0, pending: 0 });
    await waitForHostGraphSettled(page);
    await shell.locator('[data-page-id="production"]').click();
    stage = "production_planning";
    await expect.poll(() => workspaceHandle).toBeTruthy();
    await Promise.all(responseTasks);
    expect(responseErrors).toEqual([]);
    const graph = await readVisibleGraph(page);
    const nodeIds = ["1", "2", "3", "4", "5", "6", "8"];
    const ownedReference = {
      nodeIds,
      linkIds: (graph.links ?? [])
        .filter(
          (link: any) =>
            nodeIds.includes(String(link[1])) ||
            nodeIds.includes(String(link[3])),
        )
        .map((link: any) => String(link[0])),
      anchorNodeId: "7",
      authoredWidgetNodeIds: nodeIds,
    };
    await shell
      .locator('[data-h3-director-function="production_workbench"]')
      .click();
    const planning = shell.locator("[data-h3-nle-planning-status]");
    await expect(planning).toBeVisible();
    await planning
      .locator('[data-h3-nle-control="planning.target_seconds"]')
      .fill("30");
    await planning
      .locator('[data-h3-nle-control="planning.policy"]')
      .selectOption("fixed_15");
    for (const [control, state] of [
      ["prepare_context", "prepared"],
      ["admit_canonical", "admitted"],
      ["propose", "proposed"],
      ["approve_import", "imported"],
    ]) {
      const button = planning.locator(
        `[data-h3-nle-control="planning.${control}"]`,
      );
      await expect(button).toBeEnabled();
      await button.click();
      await expect(planning).toHaveAttribute(
        "data-h3-nle-planning-status",
        state!,
      );
    }
    await shell.locator('[data-h3-director-function="clip_editor"]').click();
    await shell.locator('[data-h3-nle-entry="open"]').click();
    const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
    const sequenceTab = overlay.locator('[data-h3-nle-pane="sequence"]');
    if (await sequenceTab.count()) await sequenceTab.first().click();
    await overlay.locator('[data-h3-nle-control="readiness.request"]').click();
    await expect(
      overlay.locator('[data-h3-nle-status="readiness"]'),
    ).toHaveAttribute("data-state", "ready");
    const current = await readProjection();
    const segmentIds = [...current.segments]
      .sort((a, b) => a.ordinal - b.ordinal)
      .map((row) => row.segmentId);
    expect(segmentIds).toHaveLength(2);
    expect(promptPosts).toBe(1);
    stage = "producer_install";
    producer = await installProductionRuntimeProducer(page, {
      bundleUrl,
      workspaceHandle: workspaceHandle!,
      segmentIds,
      ownedReference,
      inputFilename: fixture.inputFilename,
      runPrefix: fixture.runPrefix,
      start: "native",
      timeoutMs: 120_000,
    });
    expect((await producer.progress()).queued).toEqual([]);
    stage = "parent_start_and_collect";
    await overlay.locator('[data-h3-nle-control="sequence.start"]').click();
    const result = await producer.collect(120_000);
    expect(result.captureReceipts).toHaveLength(2);
    expect(result.sinkExecutionProofs).toHaveLength(2);
    expect(promptPosts).toBe(3);
    expect(await supportedHostQueueCounts(page)).toEqual({
      running: 0,
      pending: 0,
    });
    await mkdir(dirname(evidence), { recursive: true });
    // Keep only synthetic output locators, bounded IDs, fingerprints and counts.
    await writeFile(
      evidence,
      JSON.stringify(
        {
          schema: "managed-private-root-host-result-v1",
          phase: fixture.phase,
          status: "PASS",
          promptPosts,
          oldAuthorityRefusals,
          sinkExecutions: result.sinkExecutions,
          sinkExecutionProofs: result.sinkExecutionProofs,
          captures: result.captureReceipts.map(
            ({ runHandle: _handle, ...safe }) => safe,
          ),
        },
        null,
        2,
      ),
      { flag: "wx" },
    );
    if (fixture.phase === "before") {
      await mkdir(dirname(operational), { recursive: true });
      await writeFile(
        operational,
        JSON.stringify({
          workspaceHandle,
          runHandles: result.captureReceipts.map((row) => row.runHandle),
        }),
        { flag: "wx" },
      );
    }
    succeeded = true;
  } catch (error) {
    primaryFailure = error;
  } finally {
    const cleanupFailures: unknown[] = [];
    const cleanup = async (operation: () => Promise<unknown>) => {
      try {
        await operation();
      } catch (error) {
        cleanupFailures.push(error);
      }
    };
    // CRITICAL: snapshot before detach; cleanup can change the parent state and its
    // own 409 must never replace the first failure or erase the consumed queue budget.
    await cleanup(async () => {
      await mkdir(dirname(evidence), { recursive: true });
      await writeFile(
        `${evidence}.queue-budget.json`,
        JSON.stringify({ phase: fixture.phase, promptPosts, succeeded }),
        { flag: "wx" },
      );
      if (!succeeded) {
        await Promise.all(responseTasks);
        const diagnostic = producer
          ? await page.evaluate(() => {
              const state = (window as any).__h3StockRuntimeProducer;
              const snapshot = state.module.managedProductionSession.snapshot();
              return {
                parentSequenceId: snapshot.parentSequenceId,
                parentState: snapshot.parentState,
                parentRevision: snapshot.parentRevision,
                activeSegmentId: snapshot.activeSegmentId,
                activeQueuePromptId: snapshot.activeQueuePromptId,
                failure: snapshot.failure,
                queued: state.queues,
                executed: state.executed,
                socketFailed: state.socketFailed,
                selectionFailure: state.selectionFailure,
              };
            })
          : null;
        await writeFile(
          `${evidence}.failure.json`,
          JSON.stringify(
            {
              schema: "managed-private-root-host-failure-v1",
              phase: fixture.phase,
              status: "NOT_ACCEPTED",
              stage,
              promptPosts,
              diagnostic,
              actionStatuses,
            },
            null,
            2,
          ),
          { flag: "wx" },
        );
      }
    });
    await cleanup(async () => {
      await producer?.dispose();
    });
    // The successful before-phase keeps terminal authority only until the separately
    // authorized mandatory restart. Failure/after-phase releases its owned workspace now.
    await cleanup(async () => {
      if (workspaceHandle && (!succeeded || fixture.phase === "after")) {
        const current = await readProjection();
        const released = await post(
          page,
          PRODUCTION,
          encodeProductionAction(requestId(), "release_workspace", {
            projection: current,
          }),
        );
        expect(released.status).toBe(204);
      }
    });
    page.off("response", observe);
    await cleanup(() => page.unroute("**/prompt"));
    await cleanup(async () => {
      await Promise.all(responseTasks);
      expect(responseErrors).toEqual([]);
    });
    if (cleanupFailures.length)
      throw new AggregateError(
        primaryFailure === undefined
          ? cleanupFailures
          : [primaryFailure, ...cleanupFailures],
        "Private-storage row failed; original failure precedes cleanup failures",
      );
    if (primaryFailure !== undefined) throw primaryFailure;
  }
});
