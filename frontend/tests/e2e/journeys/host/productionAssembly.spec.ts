import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { observeOwnedGraph } from "../../../../src/host/ownedGraphIdentity";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import {
  rawAssemblyPayload,
  unavailableAssemblyFacts,
  type CandidateProductionProjection,
} from "../../host/productionAssembly";
import { expect, test } from "../../host/fixture";
import {
  assertCandidateBundleInjection,
  candidateBackendMode,
  candidateBundle,
  candidateBundleResourceUrl,
  candidateInjectionCount,
  hostUrl,
  readVisibleGraph,
  supportedHostQueueCounts,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "../../host/environment";

type SetupReceipt = Readonly<{
  workspaceHandle: string;
  workspaceId: string;
  projection: CandidateProductionProjection;
  readinessStatus: string;
  qualifiedPlan: boolean;
  qualifiedSegments: number;
  candidateActionTransports: number;
  candidateAssemblyTransports: number;
  assemblyError: string;
  activeStable: boolean;
  tabDelta: number;
}>;

test("M26-05 public assembly stays unavailable and effect-free on the supplied candidate", async ({
  page,
  context,
}, testInfo) => {
  test.setTimeout(120_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  if (candidateBundle === null || candidateBackendMode !== "exact")
    throw new Error(
      "an exact installed candidate and bundle binding are required",
    );
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });

  let promptTransports = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname.endsWith("/prompt")
    )
      promptTransports += 1;
  });
  const injections = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injections);

  const fixture = JSON.parse(
    await readFile(
      resolve(process.cwd(), "../workflows/m15_03_product_shell_base.json"),
      "utf8",
    ),
  );
  const bootstrap = structuredClone(fixture.prompt);
  delete bootstrap["7"];
  bootstrap["1"].inputs.duration_seconds = 10;
  bootstrap["1"].inputs.user_intent =
    `A blue sphere turns slowly. Assembly qualification ${Date.now()}.`;
  const promptId = await page.evaluate(
    async ({ visible, bootstrap }) => {
      const runtime = window as unknown as {
        comfyAPI: { app: { app: any }; api: { api: any } };
      };
      await runtime.comfyAPI.app.app.loadApiJson(
        visible,
        "production-assembly-qualification",
      );
      const response = await fetch("/prompt", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          prompt: bootstrap,
          client_id: runtime.comfyAPI.api.api.clientId,
        }),
      });
      if (!response.ok)
        throw new Error(`Context setup refused: ${response.status}`);
      return String((await response.json()).prompt_id);
    },
    { visible: { ...bootstrap, "7": fixture.prompt["7"] }, bootstrap },
  );
  let source: Record<string, unknown> | undefined;
  await expect
    .poll(async () => {
      const response = await page.request.get(
        new URL(`/history/${encodeURIComponent(promptId)}`, hostUrl).href,
      );
      if (!response.ok()) return false;
      source = (await response.json())[promptId]?.outputs?.["6"]
        ?.sidebar_workspace?.[0];
      return source !== undefined;
    })
    .toBe(true);
  await expect
    .poll(() => supportedHostQueueCounts(page))
    .toEqual({ running: 0, pending: 0 });
  await waitForHostGraphSettled(page);

  const before = await readVisibleGraph(page);
  const ownedIds = ["1", "2", "3", "4", "5", "6", "8"];
  const reference = {
    nodeIds: ownedIds,
    linkIds: (before.links ?? [])
      .filter(
        (link: any) =>
          ownedIds.includes(String(link[1])) ||
          ownedIds.includes(String(link[3])),
      )
      .map((link: any) => String(link[0])),
    anchorNodeId: "7",
    authoredWidgetNodeIds: ownedIds,
  };
  const ownedBefore = observeOwnedGraph(before, reference);
  const setupQueueCalls = promptTransports;
  let setup: SetupReceipt | undefined;
  let finalProjection: CandidateProductionProjection | undefined;
  try {
    setup = await page.evaluate(
      async ({ source, bundleUrl }) => {
        const runtime = window as unknown as {
          comfyAPI: { app: { app: any } };
        };
        const app = runtime.comfyAPI.app.app;
        const workflow = app.extensionManager.workflow;
        const active = workflow.activeWorkflow;
        const tabs = workflow.openWorkflows.length;
        const module = await import(/* @vite-ignore */ bundleUrl);
        const planning = module.createProductionPlanningClient({
          fetchApi: fetch,
        });
        const readiness = module.createManagedQualificationClient({
          fetchApi: fetch,
        });
        let candidateActionTransports = 0;
        let candidateAssemblyTransports = 0;
        const production = module.createProductionActionClient({
          fetchApi: (path: string, init: RequestInit) => {
            candidateActionTransports += 1;
            const action = JSON.parse(String(init.body)).action;
            if (action === "assemble_sequence")
              candidateAssemblyTransports += 1;
            return fetch(path, init);
          },
        });
        const prefix = `assembly.${crypto.randomUUID()}`;
        const rawProduction = async (
          requestId: string,
          action: string,
          payload: Record<string, unknown>,
        ) => {
          const response = await fetch("/h3-context/v1/production/action", {
            method: "POST",
            credentials: "same-origin",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              schema: "h3.context.production_workbench.action.v1",
              request_id: requestId,
              action,
              payload,
            }),
          });
          if (!response.ok)
            throw new Error(
              `Production setup refused: ${action}/${response.status}`,
            );
          return response.status === 204 ? null : response.json();
        };
        let workspace: Record<string, any> | null = null;
        try {
          workspace = await rawProduction(
            `${prefix}.create`,
            "create_workspace_from_context",
            { context_workspace_handle: source.workspace_id },
          );
          const prepared = await planning.send(
            `${prefix}.prepare`,
            "prepare_context",
            {
              workspace_handle: workspace!.workspace_handle,
              expected_workspace_revision: workspace!.workspace_revision,
              expected_workspace_fingerprint: workspace!.workspace_fingerprint,
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
          const proposed = await planning.send(`${prefix}.propose`, "propose", {
            ...module.planningSelectors(admitted),
            admission_id: admitted.admission_id,
          });
          const imported = await planning.send(
            `${prefix}.import`,
            "import_plan",
            {
              ...module.planningSelectors(proposed),
              proposal_id: proposed.proposal.proposal_id,
            },
          );
          const selection = {
            workspace_handle: workspace!.workspace_handle,
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
          const read = await production.send(
            `${prefix}.read`,
            "read_projection",
            { workspaceHandle: workspace!.workspace_handle },
          );
          let assemblyError = "";
          try {
            await production.send(`${prefix}.assemble`, "assemble_sequence", {
              projection: read.projection,
            });
          } catch (error) {
            assemblyError =
              error instanceof Error ? error.message : String(error);
          }
          return {
            workspaceHandle: workspace!.workspace_handle,
            workspaceId: workspace!.workspace_id,
            projection: read.projection,
            readinessStatus: ready.status,
            qualifiedPlan:
              ready.qualification.production_plan_fingerprint ===
              imported.plan_fingerprint,
            qualifiedSegments: intent.segmentIds.length,
            candidateActionTransports,
            candidateAssemblyTransports,
            assemblyError,
            activeStable: workflow.activeWorkflow === active,
            tabDelta: workflow.openWorkflows.length - tabs,
          };
        } catch (error) {
          if (workspace !== null) {
            const current = await rawProduction(
              `${prefix}.failed.cleanup.read`,
              "read_projection",
              { workspace_handle: workspace.workspace_handle },
            );
            await rawProduction(
              `${prefix}.failed.cleanup.release`,
              "release_workspace",
              {
                workspace_handle: current.workspace_handle,
                expected_workspace_revision: current.workspace_revision,
                expected_workspace_fingerprint: current.workspace_fingerprint,
              },
            );
          }
          throw error;
        }
      },
      { source: source!, bundleUrl: candidateBundleResourceUrl(hostUrl) },
    );

    const baseAssemblyPayload = rawAssemblyPayload(setup.projection);
    const negative = await page.evaluate(
      async ({ base, projection }) => {
        const prefix = `assembly.negative.${crypto.randomUUID()}`;
        let rawAssemblyPosts = 0;
        async function post(
          requestId: string,
          action: string,
          payload: Record<string, unknown>,
          extra: Record<string, unknown> = {},
        ) {
          if (action === "assemble_sequence") rawAssemblyPosts += 1;
          const response = await fetch("/h3-context/v1/production/action", {
            method: "POST",
            credentials: "same-origin",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              schema: "h3.context.production_workbench.action.v1",
              request_id: requestId,
              action,
              payload,
              ...extra,
            }),
          });
          const text = await response.text();
          return {
            status: response.status,
            body: text === "" ? null : JSON.parse(text),
          };
        }

        const unauthorized = await post(
          `${prefix}.unauthorized`,
          "assemble_sequence",
          base,
        );
        const stale = await post(`${prefix}.stale`, "assemble_sequence", {
          ...base,
          expected_workspace_revision: projection.workspaceRevision - 1,
        });
        const foreign = await post(`${prefix}.foreign`, "assemble_sequence", {
          ...base,
          workspace_handle: `pw_${"z".repeat(43)}`,
        });
        const malformed = await post(
          `${prefix}.malformed`,
          "assemble_sequence",
          base,
          { private_path: "closed-wire-probe" },
        );
        const replayRequestId = `${prefix}.replay`;
        const selectionPayload = {
          workspace_handle: projection.workspaceHandle,
          expected_workspace_revision: projection.workspaceRevision,
          expected_workspace_fingerprint: projection.workspaceFingerprint,
          segment_ids: projection.selectedSegmentIds,
        };
        const selected = await post(
          replayRequestId,
          "set_selection",
          selectionPayload,
        );
        const replayed = await post(
          replayRequestId,
          "set_selection",
          selectionPayload,
        );
        const conflict = await post(replayRequestId, "assemble_sequence", {
          ...base,
          expected_workspace_revision: selected.body.workspace_revision,
          expected_workspace_fingerprint: selected.body.workspace_fingerprint,
        });
        return {
          unauthorized: unauthorized.status,
          stale: stale.status,
          staleReturnedCurrent:
            stale.body?.workspace_id === projection.workspaceId,
          foreign: foreign.status,
          malformed: malformed.status,
          exactReplay:
            JSON.stringify(selected.body) === JSON.stringify(replayed.body),
          conflict: conflict.status,
          rawAssemblyPosts,
        };
      },
      { base: baseAssemblyPayload, projection: setup.projection },
    );
    expect(negative).toEqual({
      unauthorized: 409,
      stale: 409,
      staleReturnedCurrent: true,
      foreign: 404,
      malformed: 400,
      exactReplay: true,
      conflict: 409,
      rawAssemblyPosts: 5,
    });

    finalProjection = await page.evaluate(
      async ({ handle, bundleUrl }) => {
        const module = await import(/* @vite-ignore */ bundleUrl);
        const client = module.createProductionActionClient({ fetchApi: fetch });
        const read = await client.send(
          `assembly.final.${crypto.randomUUID()}`,
          "read_projection",
          { workspaceHandle: handle },
        );
        return read.projection;
      },
      {
        handle: setup.workspaceHandle,
        bundleUrl: candidateBundleResourceUrl(hostUrl),
      },
    );
  } finally {
    if (setup !== undefined) {
      await page.evaluate(
        async ({ handle, bundleUrl }) => {
          const module = await import(/* @vite-ignore */ bundleUrl);
          const client = module.createProductionActionClient({
            fetchApi: fetch,
          });
          const current = await client.send(
            `assembly.cleanup.read.${crypto.randomUUID()}`,
            "read_projection",
            { workspaceHandle: handle },
          );
          await client.send(
            `assembly.cleanup.release.${crypto.randomUUID()}`,
            "release_workspace",
            { projection: current.projection },
          );
        },
        {
          handle: setup.workspaceHandle,
          bundleUrl: candidateBundleResourceUrl(hostUrl),
        },
      );
    }
  }

  expect(setup).toBeDefined();
  expect(finalProjection).toBeDefined();
  expect(setup!.workspaceId).toBe(finalProjection!.workspaceId);
  expect(setup!.readinessStatus).toBe("ready");
  expect(setup!.qualifiedPlan).toBe(true);
  expect(setup!.qualifiedSegments).toBe(2);
  expect(setup!.candidateActionTransports).toBe(1);
  expect(setup!.candidateAssemblyTransports).toBe(0);
  expect(setup!.assemblyError).toBe("production action is not allowed");
  const initialAssembly = unavailableAssemblyFacts(setup!.projection);
  const afterAssembly = unavailableAssemblyFacts(finalProjection!);
  expect(initialAssembly).toMatchObject({
    state: "unavailable",
    capabilityFingerprint: null,
    managedSequenceFingerprint: null,
    artifactReceiptCount: 0,
    cutBoundaryReceiptCount: 0,
    assemblyJobId: null,
    authorizationFingerprint: null,
    receiptFingerprint: null,
    failureCode: "media_runtime_not_authorized",
    assemblyAdvertised: false,
  });
  expect(afterAssembly).toEqual(initialAssembly);
  expect(finalProjection!.authorityVersions).toEqual(
    setup!.projection.authorityVersions,
  );
  expect(finalProjection!.outputs).toEqual(setup!.projection.outputs);
  expect(setup!.activeStable).toBe(true);
  expect(setup!.tabDelta).toBe(0);
  expect(promptTransports).toBe(setupQueueCalls);
  expect(setupQueueCalls).toBe(1);
  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });

  const after = await readVisibleGraph(page);
  expect(observeOwnedGraph(after, reference)).toEqual(ownedBefore);
  const surroundings = diffGraphSurroundings({
    beforeValue: before,
    afterValue: after,
    reference: {
      ownedNodeIds: reference.nodeIds,
      ownedLinkIds: reference.linkIds,
      anchorNodeId: "7",
      authoredWidgetNodeIds: reference.authoredWidgetNodeIds,
      ownedProjectionEqual: true,
    },
  });
  const evidence = {
    readinessStatus: setup!.readinessStatus,
    qualifiedPlan: setup!.qualifiedPlan,
    qualifiedSegments: setup!.qualifiedSegments,
    candidateActionTransports: setup!.candidateActionTransports,
    candidateAssemblyTransports: setup!.candidateAssemblyTransports,
    unavailableAssembly: initialAssembly,
    afterAssembly,
    setupPromptTransports: setupQueueCalls,
    assemblyPromptTransports: promptTransports - setupQueueCalls,
    surroundings,
    actualAssembly: "NOT_RUN_RUNTIME_NOT_AUTHORIZED",
    acceptedAssemblyReplay: "NOT_RUN_RUNTIME_NOT_AUTHORIZED",
  };
  console.log(`M26_05_HOST_EVIDENCE=${JSON.stringify(evidence)}`);
  await testInfo.attach("production-assembly-qualification", {
    contentType: "application/json",
    body: JSON.stringify(evidence),
  });
});
