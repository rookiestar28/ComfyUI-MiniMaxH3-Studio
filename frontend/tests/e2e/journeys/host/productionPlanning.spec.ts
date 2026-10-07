import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, test } from "../../host/fixture";
import {
  assertCandidateBundleInjection,
  candidateBundleResourceUrl,
  candidateBundle,
  candidateBackendMode,
  candidateInjectionCount,
  hostUrl,
  readVisibleGraph,
  supportedHostQueueCounts,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "../../host/environment";
import { observeOwnedGraph } from "../../../../src/host/ownedGraphIdentity";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";

test("M26-03 real planning actions import canonical and reviewed rows without queue effects", async ({
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
  // The setup is a real model-free Context pipeline. Remove generation before submission,
  // never rewrite a compiled native prompt after validation and then delegate it.
  delete bootstrap["7"];
  bootstrap["1"].inputs.duration_seconds = 10;
  bootstrap["1"].inputs.user_intent =
    `A blue sphere turns slowly. Scene ${Date.now()}.`;
  const promptId = await page.evaluate(
    async ({ visible, bootstrap }) => {
      const runtime = window as unknown as {
        comfyAPI: { app: { app: any }; api: { api: any } };
      };
      await runtime.comfyAPI.app.app.loadApiJson(
        visible,
        "production-planning-context",
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
      const row = (await response.json())[promptId];
      source = row?.outputs?.["6"]?.sidebar_workspace?.[0];
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
  const queueBefore = promptTransports;
  const receipt = await page.evaluate(
    async ({ source, bundleUrl }) => {
      const runtime = window as unknown as {
        comfyAPI: { app: { app: any }; api: { api: any } };
      };
      const app = runtime.comfyAPI.app.app;
      const workflow = app.extensionManager.workflow;
      const active = workflow.activeWorkflow;
      const tabs = workflow.openWorkflows.length;
      const module = await import(/* @vite-ignore */ bundleUrl);
      const client = module.createProductionPlanningClient({
        fetchApi: (path: string, init: RequestInit) => fetch(path, init),
      });
      const selectors = module.planningSelectors;
      const postProduction = async (
        request_id: string,
        action: string,
        payload: Record<string, unknown>,
      ) => {
        const response = await fetch("/h3-context/v1/production/action", {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            schema: "h3.context.production_workbench.action.v1",
            request_id,
            action,
            payload,
          }),
        });
        if (!response.ok)
          throw new Error(`Production refused: ${response.status}`);
        return response.status === 204 ? null : response.json();
      };
      const results = [];
      for (const canonical of [true, false]) {
        const prefix = `planning.${crypto.randomUUID()}`;
        const workspace = await postProduction(
          `${prefix}.create`,
          "create_workspace_from_context",
          { context_workspace_handle: source.workspace_id },
        );
        try {
          const prepared = await client.send(
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
          const admitted = await client.send(
            `${prefix}.admit`,
            "admit_storyboard",
            {
              ...selectors(prepared),
              source_kind: canonical
                ? "canonical_optimized_prompt"
                : "user_reviewed_typed_rows",
              user_reviewed: !canonical,
              typed_rows: canonical
                ? []
                : [
                    {
                      schema: "h3.context.storyboard_shot.v1",
                      shot_id: "reviewed_1",
                      ordinal: 1,
                      start_milliseconds: 0,
                      end_milliseconds: 10000,
                      text: "A blue sphere turns slowly.",
                      subject_ids: [],
                      asset_ids: [],
                      exact_dialogue: [],
                      visible_text: [],
                      source_span: [0, 0],
                      hard_boundary: false,
                    },
                    {
                      schema: "h3.context.storyboard_shot.v1",
                      shot_id: "reviewed_2",
                      ordinal: 2,
                      start_milliseconds: 10000,
                      end_milliseconds: 20000,
                      text: "The same sphere stops.",
                      subject_ids: [],
                      asset_ids: [],
                      exact_dialogue: [],
                      visible_text: [],
                      source_span: [0, 0],
                      hard_boundary: false,
                    },
                  ],
            },
          );
          const proposed = await client.send(`${prefix}.propose`, "propose", {
            ...selectors(admitted),
            admission_id: admitted.admission_id,
          });
          const read = await client.send(
            `${prefix}.read`,
            "read_plan",
            selectors(proposed),
          );
          const payload = {
            ...selectors(proposed),
            proposal_id: proposed.proposal.proposal_id,
          };
          const imported = await client.send(
            `${prefix}.import`,
            "import_plan",
            payload,
          );
          const replay = await client.send(
            `${prefix}.import`,
            "import_plan",
            payload,
          );
          results.push({
            canonical,
            sourceDuration: prepared.source_duration_seconds,
            targetDuration: prepared.target_seconds,
            segmentCount: proposed.proposal.segments.length,
            receiptCount: imported.materialization_receipt_fingerprints.length,
            readStable:
              read.proposal.fingerprint === proposed.proposal.fingerprint,
            exactReplay: JSON.stringify(imported) === JSON.stringify(replay),
            revision: imported.workspace_revision,
          });
        } finally {
          // A successful import can lose its response. Recover the exact owned workspace's
          // current CAS before cleanup instead of hiding the failure behind a stale release.
          const current = await postProduction(
            `${prefix}.cleanup.read`,
            "read_projection",
            {
              workspace_handle: workspace.workspace_handle,
            },
          );
          if (current.workspace_id !== workspace.workspace_id)
            throw new Error("cleanup workspace identity changed");
          await postProduction(`${prefix}.release`, "release_workspace", {
            workspace_handle: current.workspace_handle,
            expected_workspace_revision: current.workspace_revision,
            expected_workspace_fingerprint: current.workspace_fingerprint,
          });
        }
      }
      return {
        results,
        activeStable: workflow.activeWorkflow === active,
        tabDelta: workflow.openWorkflows.length - tabs,
        extensionCount: app.extensions.length,
      };
    },
    { source: source!, bundleUrl: candidateBundleResourceUrl(hostUrl) },
  );
  expect(receipt.results).toEqual(
    [true, false].map((canonical) => ({
      canonical,
      sourceDuration: 10,
      targetDuration: 20,
      segmentCount: 2,
      receiptCount: 2,
      readStable: true,
      exactReplay: true,
      revision: 2,
    })),
  );
  expect(receipt.activeStable).toBe(true);
  expect(receipt.tabDelta).toBe(0);
  expect(promptTransports - queueBefore).toBe(0);
  expect(queueBefore).toBe(1);
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
    ...receipt,
    setupPromptTransports: queueBefore,
    planningPromptTransports: promptTransports - queueBefore,
    surroundings,
  };
  // The host lane intentionally discards browser artifacts; retain only this content-free
  // receipt in its command log, never a workflow, response payload or prompt.
  console.log(`M26_03_HOST_EVIDENCE=${JSON.stringify(evidence)}`);
  await testInfo.attach("production-planning-receipt", {
    contentType: "application/json",
    body: JSON.stringify(evidence),
  });
});
