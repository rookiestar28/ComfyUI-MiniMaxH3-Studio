import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import {
  decodeProductionWorkbenchProjection,
  encodeProductionAction,
  type ProductionWorkbenchProjection,
} from "../../../../src/contracts/productionWorkbenchCodec";
import { decodeProductionAuthoringImportResponseV1 as decodeProductionAuthoringImportResponse } from "../../../../src/contracts/productionAuthoringImportCodec";
import {
  decodeTimelineHistoryProjection,
  encodeAuthoringAction,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import {
  decodeOutputStatus,
  OUTPUT_CAPABILITY,
} from "../../../../src/contracts/authoringOutputCodec";
import { normalizeM2508HostApiPath } from "../../host/m25_08RequestClassification";
import {
  AUTHORING_MEDIA_LEASE_ROUTE,
  AUTHORING_MEDIA_LEASE_OPEN_ROUTE,
} from "../../../../src/host/authoringMediaSourceLease";
import { setSupportedH3Language } from "../../host/layout";
import { observeOwnedGraph } from "../../../../src/host/ownedGraphIdentity";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import { runProductionRuntimeProducer } from "../../host/productionRuntimeProducer";
import {
  createOwnerCapture,
  ownedPath,
  post,
  readReadyPreviewBytes,
} from "../../host/productionRuntimeRow";
import {
  ensureM2508CapturedWorkflowAuthority,
  materializeM2508VisiblePromptInCapturedWorkflow,
} from "../../host/m25_08Bootstrap";
import { openExportPanel } from "../../helpers/nleExport";
import { expect, test } from "../../host/fixture";
import {
  assertCandidateBundleInjection,
  candidateBackendMode,
  candidateBundle,
  candidateBundleResourceUrl,
  candidateInjectionCount,
  hostUrl,
  openH3AppModeTab,
  readVisibleGraph,
  repositoryRoot,
  supportedHostQueueCounts,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "../../host/environment";

type RuntimeFixture = {
  schema: "ProductionRuntimeIntegrationFixtureV1";
  inputFilenames: string[];
  runPrefix: string;
  evidenceRelative: string;
  apparatusRelative: string;
};

// This row consumes actual stock-node execution and canonical artifact capture. The explicit
// opt-in keeps an ordinary hermetic run from touching a supplied host or private apparatus.
test("automatic assembly retains original outputs for explicit NLE import and rendering", async ({
  page,
  context,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_PRODUCTION_RUNTIME_INTEGRATION !== "1",
    "explicit supplied-host runtime qualification required",
  );
  test.setTimeout(600_000);
  if (!hostUrl || !candidateBundle || candidateBackendMode !== "exact")
    throw new Error(
      "an exact installed candidate and supplied host are required",
    );
  const fixtureValue = process.env.H3_CONTEXT_PRODUCTION_RUNTIME_FIXTURE;
  if (!fixtureValue) throw new Error("runtime fixture is required");
  const fixture: RuntimeFixture = JSON.parse(
    await readFile(ownedPath(fixtureValue), "utf8"),
  );
  if (
    fixture.schema !== "ProductionRuntimeIntegrationFixtureV1" ||
    fixture.inputFilenames.length !== 2 ||
    !fixture.inputFilenames.every((name) =>
      /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name),
    ) ||
    !/^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$/.test(fixture.runPrefix)
  )
    throw new Error("invalid two-child runtime fixture");
  const evidence = ownedPath(fixture.evidenceRelative);
  const apparatus = ownedPath(fixture.apparatusRelative);
  await mkdir(evidence, { recursive: true });
  const ownerCapture = await createOwnerCapture(apparatus, evidence);
  const route = "/h3-context/v1/production/action";
  let serial = 0;
  const requestId = (name: string) =>
    `${fixture.runPrefix}.${name}.${++serial}`;
  const readProduction = async (handle: string) => {
    const response = await post(
      page,
      route,
      encodeProductionAction(requestId("read"), "read_projection", {
        workspaceHandle: handle,
      }),
    );
    expect(response.status, JSON.stringify(response.body)).toBe(200);
    return decodeProductionWorkbenchProjection(response.body);
  };
  let productionHandle: string | undefined;
  let authoringHandle: string | undefined;
  let observationStarted = false;
  let managedProducerStarted = false;
  let priorParentId: string | null = null;
  let primaryFailure: unknown;
  const responseTasks: Promise<void>[] = [];
  const responseErrors: unknown[] = [];
  const imports: Array<{ request: any; status: number; body: any }> = [];
  const authoringResponses: Array<{
    action: string;
    status: number;
    contextWorkspaceHandle?: string;
    body: any;
  }> = [];
  const outputStatuses: ReturnType<typeof decodeOutputStatus>[] = [];
  const cleanup: Record<string, unknown> = {};
  const observations: Record<string, unknown> = {
    candidateBundleSha256: candidateBundle.sha256,
  };
  let promptPosts = 0;
  const productionWrites: string[] = [];
  const productionResponses: Array<{
    action: string;
    status: number;
    body: any;
  }> = [];
  const runtimeResponses: Array<Record<string, unknown>> = [];
  const leaseResponses: Array<Record<string, unknown>> = [];
  const onRequest = (request: import("@playwright/test").Request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path.endsWith("/prompt")) promptPosts++;
    if (request.method() === "POST" && path.endsWith(route))
      productionWrites.push(String(request.postDataJSON()?.action));
  };
  const onResponse = (response: import("@playwright/test").Response) => {
    const path = normalizeM2508HostApiPath(new URL(response.url()).pathname);
    if (
      ![
        route,
        "/h3-context/v1/managed-sequences",
        "/h3-context/v1/generation/coordinator",
        "/h3-context/v1/production/authoring-import",
        "/h3-context/v1/authoring/action",
        AUTHORING_MEDIA_LEASE_ROUTE,
        AUTHORING_MEDIA_LEASE_OPEN_ROUTE,
      ].includes(path) &&
      path !== OUTPUT_CAPABILITY.job_path &&
      !path.startsWith(`${OUTPUT_CAPABILITY.job_path}/`)
    )
      return;
    responseTasks.push(
      (async () => {
        if (
          path === AUTHORING_MEDIA_LEASE_ROUTE ||
          path === AUTHORING_MEDIA_LEASE_OPEN_ROUTE
        ) {
          // Keep only closed protocol dispositions; lease capabilities and media bodies stay private.
          const body =
            path === AUTHORING_MEDIA_LEASE_ROUTE || !response.ok()
              ? await response.json()
              : null;
          leaseResponses.push({
            operation: response.request().postDataJSON()?.operation,
            status: response.status(),
            reason:
              typeof body?.reason === "string" &&
              /^[a-z_]{1,60}$/.test(body.reason)
                ? body.reason
                : null,
            byteCount: Number.isSafeInteger(body?.byteCount)
              ? body.byteCount
              : null,
          });
          return;
        }
        if (
          path === "/h3-context/v1/managed-sequences" ||
          path === "/h3-context/v1/generation/coordinator"
        ) {
          const body = await response.json();
          const request = response.request().postDataJSON();
          // Retain bounded protocol dispositions, not prompt text or authority payloads.
          runtimeResponses.push({
            path,
            action: request?.action,
            status: response.status(),
            disposition: Object.fromEntries(
              Object.entries(body).filter(
                ([key, value]) =>
                  [
                    "schema",
                    "code",
                    "error",
                    "reason",
                    "failure_code",
                    "state",
                    "status",
                    "revision",
                  ].includes(key) &&
                  (typeof value === "number" ||
                    typeof value === "boolean" ||
                    value === null ||
                    (typeof value === "string" &&
                      /^[A-Za-z0-9_.:-]{1,180}$/.test(value))),
              ),
            ),
          });
        } else if (path === route) {
          const action = String(response.request().postDataJSON()?.action);
          const body = response.status() === 204 ? null : await response.json();
          // IMPORTANT: learn cleanup ownership from the actual Shell response even if the
          // next assertion fails. A late waiter cannot prove that no workspace was created.
          if (
            action === "create_workspace_from_context" &&
            response.status() === 201
          )
            productionHandle =
              decodeProductionWorkbenchProjection(body).workspaceHandle;
          productionResponses.push({ action, status: response.status(), body });
        } else if (path.endsWith("/production/authoring-import")) {
          const imported = {
            request: response.request().postDataJSON(),
            status: response.status(),
            body: null as unknown,
          };
          try {
            // IMPORTANT: import refusals are status-only. Parsing their absent body masks
            // the real refusal as a CDP body error and strands the receipt-count waiter.
            if (imported.status === 200) imported.body = await response.json();
          } finally {
            imports.push(imported);
          }
        } else if (path.endsWith("/authoring/action")) {
          const request = response.request().postDataJSON();
          const action = request?.action;
          const body = response.status() === 204 ? null : await response.json();
          if (
            action === "create_authoring_workspace" &&
            response.status() === 201
          )
            authoringHandle = body.workspace_handle;
          authoringResponses.push({
            action,
            status: response.status(),
            ...(action === "create_authoring_workspace"
              ? {
                  contextWorkspaceHandle:
                    request.payload?.context_workspace_handle,
                }
              : {}),
            body,
          });
        } else if (response.ok())
          outputStatuses.push(decodeOutputStatus(await response.json()));
      })().catch((error: unknown) => {
        responseErrors.push(error);
      }),
    );
  };
  page.on("request", onRequest);
  page.on("response", onResponse);
  try {
    expect(await supportedHostQueueCounts(page)).toEqual({
      running: 0,
      pending: 0,
    });
    const injections = candidateInjectionCount(context);
    await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
    await waitForH3Registration(page);
    await assertCandidateBundleInjection(page, context, injections);
    await page.evaluate(() => {
      const runtime = window as unknown as Record<string, unknown>;
      runtime.__h3ProjectionTrace = [];
      runtime.__h3HostProjectionTrace = [];
    });
    await setSupportedH3Language(page, "en");
    const shell = await openH3AppModeTab(
      page,
      "h3-context-production-runtime-integration",
    );
    const sourceFixture = JSON.parse(
      await readFile(
        resolve(repositoryRoot, "workflows/m15_03_product_shell_base.json"),
        "utf8",
      ),
    );
    const bootstrap = structuredClone(sourceFixture.prompt);
    delete bootstrap["7"];
    bootstrap["1"].inputs.duration_seconds = 15;
    bootstrap["1"].inputs.user_intent =
      `Synthetic moving color landmarks with a steady tone. ${fixture.runPrefix}.`;
    const contextOnly = new Set(
      [
        "Request",
        "Plan",
        "Compiler",
        "Validator",
        "NativeH3Adapter",
        "ProductShell",
        "Preview",
      ].map((name) => `comfyui_h3_context.H3Context.${name}`),
    );
    if (
      !Object.values(bootstrap).every((node: any) =>
        contextOnly.has(node.class_type),
      )
    )
      throw new Error(
        "context bootstrap includes an unauthorized execution node",
      );
    // IMPORTANT: ComfyUI's existing workflow tracker can erase a raw loadApiJson graph
    // during activation. Materialize through the captured workflow before execution.
    await page.evaluate(materializeM2508VisiblePromptInCapturedWorkflow, {
      ...bootstrap,
      "7": sourceFixture.prompt["7"],
    });
    await page.evaluate(ensureM2508CapturedWorkflowAuthority);
    await waitForHostGraphSettled(page);
    observations.contextGraphBeforeQueue = observeOwnedGraph(
      await readVisibleGraph(page),
      {
        nodeIds: ["1", "2", "3", "4", "5", "6", "8"],
        linkIds: [],
        anchorNodeId: "7",
        authoredWidgetNodeIds: ["1", "2", "3", "4", "5", "6", "8"],
      },
    );
    const bootstrapPrompt = await page.evaluate(async (bootstrap) => {
      const runtime = window as unknown as {
        comfyAPI: { app: { app: any }; api: { api: any } };
      };
      const response = await fetch("/prompt", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          prompt: bootstrap,
          client_id: runtime.comfyAPI.api.api.clientId,
        }),
      });
      if (!response.ok)
        throw new Error(`context bootstrap refused: ${response.status}`);
      return String((await response.json()).prompt_id);
    }, bootstrap);
    let source: Record<string, any> | undefined;
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            new URL(`/history/${encodeURIComponent(bootstrapPrompt)}`, hostUrl)
              .href,
          );
          source = (await response.json())[bootstrapPrompt]?.outputs?.["6"]
            ?.sidebar_workspace?.[0];
          return source !== undefined;
        },
        { timeout: 60_000 },
      )
      .toBe(true);
    await expect
      .poll(() => supportedHostQueueCounts(page))
      .toEqual({ running: 0, pending: 0 });
    observations.bootstrapContextWorkspaceHandle = source!.workspace_id;
    await waitForHostGraphSettled(page);
    await shell.locator('[data-page-id="production"]').click();
    await expect.poll(() => productionHandle, { timeout: 30_000 }).toBeTruthy();
    const created = productionResponses.find(
      (response) =>
        response.action === "create_workspace_from_context" &&
        response.status === 201,
    );
    expect(created).toBeDefined();
    const workspace = decodeProductionWorkbenchProjection(created!.body);
    productionHandle = workspace.workspaceHandle;
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
    const prepared = await page.evaluate(
      async ({ source, workspace, bundleUrl, prefix }) => {
        const module = await import(/* @vite-ignore */ bundleUrl);
        const planning = module.createProductionPlanningClient({
          fetchApi: fetch,
        });
        const readiness = module.createManagedQualificationClient({
          fetchApi: fetch,
        });
        const context = await planning.send(
          `${prefix}.prepare`,
          "prepare_context",
          {
            workspace_handle: workspace.workspaceHandle,
            expected_workspace_revision: workspace.workspaceRevision,
            expected_workspace_fingerprint: workspace.workspaceFingerprint,
            context_workspace_handle: source.workspace_id,
            expected_report_revision: source.report_revision,
            expected_report_fingerprint: source.report_fingerprint,
            expected_planning_revision: 0,
            target_seconds: 30,
            policy: "fixed_15",
          },
        );
        const admitted = await planning.send(
          `${prefix}.admit`,
          "admit_storyboard",
          {
            ...module.planningSelectors(context),
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
          workspace_handle: workspace.workspaceHandle,
          expected_workspace_revision: imported.workspace_revision,
          expected_workspace_fingerprint: imported.workspace_fingerprint,
          expected_plan_fingerprint: imported.plan_fingerprint,
        };
        const ready = await readiness.send(
          `${prefix}.ready`,
          "prepare_managed_readiness",
          selection,
        );
        if (ready.status !== "ready")
          throw new Error(`managed readiness refused: ${ready.reason}`);
        return {
          intent: module.buildQualifiedManagedStartIntent(
            ready,
            selection,
            imported,
            proposed.proposal.fingerprint,
          ),
          readiness: ready.status,
        };
      },
      {
        source: source!,
        workspace,
        bundleUrl: candidateBundleResourceUrl(hostUrl),
        prefix: fixture.runPrefix,
      },
    );
    expect(promptPosts).toBe(1);
    expect(
      productionWrites.filter((action) => action === "assemble_sequence"),
    ).toHaveLength(0);
    priorParentId = await page.evaluate(async (bundleUrl) => {
      const module = await import(/* @vite-ignore */ bundleUrl);
      const snapshot = module.managedProductionSession.snapshot();
      if (
        snapshot.parentSequenceId &&
        !["succeeded", "cancelled"].includes(snapshot.parentState)
      )
        throw new Error(
          "runtime row cannot adopt an existing active managed parent",
        );
      return snapshot.parentSequenceId;
    }, candidateBundleResourceUrl(hostUrl));
    managedProducerStarted = true;
    const producer = await runProductionRuntimeProducer(page, {
      bundleUrl: candidateBundleResourceUrl(hostUrl),
      intent: prepared.intent,
      workspaceHandle: productionHandle,
      ownedReference,
      inputFilename: fixture.inputFilenames,
      runPrefix: fixture.runPrefix,
      timeoutMs: 180_000,
    });
    observations.producer = producer;
    observations.contextSetupPromptId = bootstrapPrompt;
    expect(producer.childPromptIds).toHaveLength(2);
    expect(new Set(producer.childPromptIds).size).toBe(2);
    expect(producer.sinkExecutions).toHaveLength(2);
    expect(producer.sinkExecutionProofs).toHaveLength(2);
    for (const [index, proof] of producer.sinkExecutionProofs.entries()) {
      expect(proof.promptId).toBe(producer.childPromptIds[index]);
      expect(proof.executingSequence).toBeLessThan(proof.executedSequence);
      if (index > 0)
        expect(
          producer.sinkExecutionProofs[index - 1].executedSequence,
        ).toBeLessThan(proof.executingSequence);
      expect(
        producer.sinkExecutions.filter(
          (row) =>
            row.promptId === proof.promptId &&
            row.outputNodeId === proof.outputNodeId,
        ),
      ).toHaveLength(1);
      expect(
        producer.captureReceipts.filter(
          (row) =>
            row.promptId === proof.promptId &&
            row.outputNodeId === proof.outputNodeId,
        ),
      ).toHaveLength(1);
    }
    expect(producer.captureReceipts).toHaveLength(2);
    expect(promptPosts).toBe(3);
    let current = await readProduction(productionHandle);
    expect(current.runState).toBe("succeeded");
    expect(
      current.outputs.filter(
        (output) => output.segmentId !== null && output.state === "ready",
      ),
    ).toHaveLength(2);
    expect(current.assembly.state).toBe("unavailable");
    expect(current.allowedActions).toContain("assemble_sequence");
    expect(
      productionWrites.filter((action) => action === "assemble_sequence"),
    ).toHaveLength(0);
    const originals = current.outputs.filter(
      (output) => output.segmentId !== null,
    );
    // Even an uncertain observe response may have installed the wrapper; always send stop.
    observationStarted = true;
    const observation = await ownerCapture("observe", current);
    expect(observation.capture).toEqual({
      observerInstalled: true,
      workerSubmissions: 0,
    });
    // IMPORTANT: replay the exact originally accepted request. Encoding it from the newer
    // succeeded projection would test a different CAS and cannot prove effect-free replay.
    const assemblyRequest = encodeProductionAction(
      `${fixture.runPrefix}.assemble`,
      "assemble_sequence",
      { projection: current },
    );
    const assemblyStart = await post(page, route, assemblyRequest);
    expect(assemblyStart.status, JSON.stringify(assemblyStart.body)).toBe(200);
    await expect
      .poll(
        async () => {
          current = await readProduction(productionHandle!);
          if (current.assembly.state === "failed")
            throw new Error(
              `assembly failed: ${JSON.stringify(current.assembly)}`,
            );
          return current.assembly.state;
        },
        { timeout: 180_000, intervals: [250, 500, 1000] },
      )
      .toBe("succeeded");
    const assembled = current;
    expect(
      assembled.outputs.filter((output) => output.segmentId !== null),
    ).toEqual(originals);
    const aggregate = assembled.outputs.find(
      (output) => output.segmentId === null,
    );
    expect(aggregate).toMatchObject({ state: "ready", preview: true });
    const beforeReplay = await ownerCapture("capture", assembled);
    expect(beforeReplay.capture.workerSubmissions).toBe(1);
    expect(beforeReplay.capture.submissions).toEqual([
      {
        jobId: assembled.assembly.assemblyJobId,
        authorizationFingerprint: assembled.assembly.authorizationFingerprint,
        returned: true,
        raised: false,
      },
    ]);
    expect(beforeReplay.capture.persistentOutputInspection).toBe("complete");
    expect(beforeReplay.capture.persistentOutputCount).toBe(
      beforeReplay.capture.reconstructionReceipt.outputs.length,
    );
    expect(beforeReplay.capture.persistentOutputCount).toBeGreaterThan(0);
    expect(
      beforeReplay.capture.originalArtifactReceipts
        .map((row: any) => row.receipt_fingerprint)
        .sort(),
    ).toEqual(
      producer.captureReceipts.map((row) => row.receiptFingerprint).sort(),
    );
    expect(
      decodeProductionWorkbenchProjection(beforeReplay.capture.projection),
    ).toEqual(assembled);
    const replay = await post(page, route, assemblyRequest);
    expect(replay.status, JSON.stringify(replay.body)).toBe(200);
    expect(decodeProductionWorkbenchProjection(replay.body)).toEqual(assembled);
    const afterReplay = await ownerCapture("capture", assembled);
    // Real submission observations and persisted receipt bodies must remain identical,
    // including the byte-backed outputs. A saved pair of counters alone proves no replay join.
    expect(afterReplay.capture).toEqual(beforeReplay.capture);
    observations.assembly = {
      projection: assembled,
      beforeReplay,
      afterReplay,
    };
    const refreshed = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith(route) &&
        response.request().postDataJSON()?.action === "read_projection",
    );
    await shell.locator('[data-h3-focus-key="production-refetch"]').click();
    expect((await refreshed).status()).toBe(200);
    const aggregateResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith(
          "/production/media-preview",
        ) &&
        response.request().postDataJSON()?.output_handle ===
          aggregate!.outputHandle,
    );
    await shell
      .locator('.h3p-o button[aria-controls="h3p-preview-panel"]')
      .click();
    const previewResponse = await aggregateResponse;
    expect(previewResponse.status()).toBe(200);
    const previewHeaders = await previewResponse.allHeaders();
    observations.aggregatePreview = {
      status: previewResponse.status(),
      byteObservation: "ready_player_blob",
      headers: Object.fromEntries(
        [
          "content-type",
          "cache-control",
          "content-disposition",
          "x-content-type-options",
          "content-length",
        ].map((key) => [key, previewHeaders[key] ?? null]),
      ),
    };
    // The preview also owns a hidden sampling video; observe the user's labeled player.
    const player = shell.getByLabel("Aggregate output preview", {
      exact: true,
    });
    const aggregateBytes = await readReadyPreviewBytes(
      player,
      previewResponse,
      8 * 1024 * 1024,
    );
    expect(aggregateBytes.length).toBeGreaterThan(0);
    await writeFile(resolve(evidence, "aggregate-preview.mp4"), aggregateBytes);
    Object.assign(observations.aggregatePreview as object, {
      sha256: createHash("sha256").update(aggregateBytes).digest("hex"),
      bytes: aggregateBytes.length,
    });
    expect(
      await player.evaluate((node: HTMLVideoElement) => node.duration),
    ).toBeCloseTo(30, 1);
    // Select only one original through the real UI. Its structural CAS legitimately changes;
    // the retained completed assembly must still describe the same accepted operation.
    const selected = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith(route) &&
        response.request().postDataJSON()?.action === "set_selection",
    );
    await shell
      .locator('[data-testid="production-timeline-segment"] button')
      .first()
      .click();
    expect((await selected).status()).toBe(200);
    current = await readProduction(productionHandle);
    expect(current.selectedSegmentIds).toEqual([originals[0]!.segmentId]);
    expect(current.assembly.assemblyJobId).toBe(
      assembled.assembly.assemblyJobId,
    );
    expect(current.assembly.state).toBe("succeeded");
    expect(current.allowedActions).toContain(
      "import_production_outputs_to_authoring",
    );
    const beforeImportGraph = await readVisibleGraph(page);
    const beforeImportOwned = observeOwnedGraph(
      beforeImportGraph,
      producer.ownedReference,
    );
    await shell
      .locator('[data-h3-nle-control="asset.import_production"]')
      .click();
    await expect.poll(() => imports.length, { timeout: 60_000 }).toBe(1);
    expect(imports[0]!.status, JSON.stringify(imports[0]!.body)).toBe(200);
    await expect
      .poll(() =>
        authoringResponses.some(
          (row) => row.action === "read_timeline_history",
        ),
      )
      .toBe(true);
    const imported = decodeProductionAuthoringImportResponse(imports[0]!.body);
    authoringHandle = imported.receipt.authoringWorkspaceHandle;
    expect(imported.authoringProjection.workspaceHandle).toBe(authoringHandle);
    expect(imported.receipt.productionWorkspaceId).toBe(current.workspaceId);
    expect(imported.receipt.productionWorkspaceRevision).toBe(
      current.workspaceRevision,
    );
    expect(imported.receipt.productionWorkspaceFingerprint).toBe(
      current.workspaceFingerprint,
    );
    expect(imports[0]!.request.entries).toEqual([
      {
        segment_id: originals[0]!.segmentId,
        output_handle: originals[0]!.outputHandle,
      },
    ]);
    expect(imported.receipt.rows).toHaveLength(1);
    expect(imported.receipt.nle.nextTimelineRevision).toBe(
      imported.receipt.nle.priorTimelineRevision,
    );
    const history = decodeTimelineHistoryProjection(
      authoringResponses.find((row) => row.action === "read_timeline_history")!
        .body,
    );
    expect(history.snapshot.clips).toHaveLength(0);
    const assetId = imported.receipt.rows[0]!.assetId;
    const initializedHistory = decodeTimelineHistoryProjection(
      authoringResponses.find(
        (row) => row.action === "initialize_timeline_history",
      )!.body,
    );
    // IMPORTANT: initialization includes packaged fonts; a total of one falsely rejects import.
    // Preserve the existing catalog and assert the exact receipt-owned video delta separately.
    expect(
      history.snapshot.assets.filter((asset) => asset.assetId !== assetId),
    ).toEqual(initializedHistory.snapshot.assets);
    expect(
      history.snapshot.assets.filter((asset) => asset.assetId === assetId),
    ).toEqual([expect.objectContaining({ assetId, kind: "video" })]);
    await shell.locator('[data-h3-nle-entry="open"]').click();
    const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
    await expect(overlay).toBeVisible();
    const importedCard = overlay.locator(`[data-h3-nle-asset="${assetId}"]`);
    await expect(importedCard).toHaveAttribute("data-highlighted", "true");
    const insertResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith("/authoring/action") &&
        response.request().postDataJSON()?.action ===
          "apply_timeline_transaction",
    );
    await importedCard.locator('[data-h3-nle-control="asset.insert"]').click();
    const inserted = await insertResponse;
    expect(inserted.status()).toBe(200);
    expect(
      inserted
        .request()
        .postDataJSON()
        .payload.commands.map((row: any) => row.kind),
    ).toEqual(["insert_asset_clip"]);
    await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);
    const play = overlay.locator('[data-h3-nle-control="transport.play"]');
    await expect(play).toBeEnabled();
    const pixels = () =>
      overlay.locator("canvas").evaluate((canvas: HTMLCanvasElement) => {
        const data = canvas
          .getContext("2d")!
          .getImageData(0, 0, canvas.width, canvas.height).data;
        let checksum = 2166136261,
          colors = 0;
        for (let i = 0; i < data.length; i += 64) {
          checksum = Math.imul(checksum ^ data[i]!, 16777619);
          if (data[i]! + data[i + 1]! + data[i + 2]! > 40) colors++;
        }
        return { checksum: checksum >>> 0, colors };
      });
    await expect.poll(async () => (await pixels()).colors).toBeGreaterThan(100);
    const first = await pixels();
    const seek = overlay.locator('[data-h3-nle-control="transport.seek"]');
    await seek.press("Home");
    for (let frame = 0; frame < 12; frame++) await seek.press("ArrowRight");
    await expect(seek).toHaveValue("12");
    await expect
      .poll(async () => (await pixels()).checksum)
      .not.toBe(first.checksum);
    await play.click();
    await expect(
      overlay.locator('[data-h3-nle-status="audio"]'),
    ).toHaveAttribute("data-h3-nle-audio-state", "following");
    await expect
      .poll(async () => Number(await seek.inputValue()))
      .toBeGreaterThan(12);
    await overlay.locator('[data-h3-nle-control="transport.pause"]').click();
    // Pause must stop advancement, not only accept the click: the position settles, then holds
    // across a fixed observation window.
    let pausedFrame = -1;
    await expect
      .poll(async () => {
        const before = Number(await seek.inputValue());
        await page.waitForTimeout(250);
        pausedFrame = Number(await seek.inputValue());
        return pausedFrame === before;
      })
      .toBe(true);
    await page.waitForTimeout(1_000);
    await expect(seek).toHaveValue(String(pausedFrame));
    observations.transport = { seekFrame: 12, pausedFrame };
    // M25-44: the final-video card lives in the chrome bar's Export popover.
    await openExportPanel(overlay);
    const render = overlay.locator('[data-h3-nle-region="render"]');
    await expect(render).toHaveAttribute("data-h3-nle-render", "available");
    await render
      .getByRole("button", { name: "Render final video", exact: true })
      .click();
    await expect(render.getByRole("status").first()).toHaveText("Video ready", {
      timeout: 180_000,
    });
    await expect
      .poll(() =>
        outputStatuses.some(
          (row) =>
            row.phase === "succeeded" &&
            row.workspace_handle === authoringHandle,
        ),
      )
      .toBe(true);
    const outputStatus = [...outputStatuses]
      .reverse()
      .find(
        (row) =>
          row.phase === "succeeded" && row.workspace_handle === authoringHandle,
      )!;
    expect(outputStatus.availability).toBe("available");
    expect(outputStatus.output_handle).not.toBeNull();
    const outputResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === "GET" &&
        normalizeM2508HostApiPath(url.pathname) ===
          `${OUTPUT_CAPABILITY.output_path}/${outputStatus.output_handle}/preview` &&
        url.searchParams.get("workspace_handle") === authoringHandle
      );
    });
    await render
      .getByRole("button", { name: "Preview output", exact: true })
      .click();
    const output = await outputResponse;
    expect(output.status()).toBe(200);
    expect(output.headers()["content-type"]).toBe("video/mp4");
    const outputBytes = await readReadyPreviewBytes(
      render.getByLabel("Final video preview", { exact: true }),
      output,
      OUTPUT_CAPABILITY.max_preview_bytes,
    );
    await writeFile(resolve(evidence, "nle-render-preview.mp4"), outputBytes);
    observations.nle = {
      importReceipt: imported.receipt,
      insertion: await inserted.json(),
      outputStatus,
      renderedBytes: outputBytes.length,
      renderedSha256: createHash("sha256").update(outputBytes).digest("hex"),
      previewPixelsChanged: true,
    };
    await overlay.locator('[data-h3-nle-action="close"]').click();
    const afterImportGraph = await readVisibleGraph(page);
    expect(
      observeOwnedGraph(afterImportGraph, producer.ownedReference),
    ).toEqual(beforeImportOwned);
    observations.surroundings = diffGraphSurroundings({
      beforeValue: beforeImportGraph,
      afterValue: afterImportGraph,
      reference: {
        ownedNodeIds: producer.ownedReference.nodeIds,
        ownedLinkIds: producer.ownedReference.linkIds,
        anchorNodeId: producer.ownedReference.anchorNodeId,
        ownedProjectionEqual: true,
      },
    });
    expect(promptPosts).toBe(3);
    expect(await supportedHostQueueCounts(page)).toEqual({
      running: 0,
      pending: 0,
    });
    observations.queue = {
      contextSetup: 1,
      managedChildren: 2,
      afterCompletion: 0,
    };
  } catch (error) {
    primaryFailure = error;
    observations.monitorAtFailure = await page
      .evaluate(() => ({
        // This node contains only repository-owned localized transport status, never user text.
        status:
          document
            .querySelector('[data-h3-nle-status="monitor"]')
            ?.textContent?.slice(0, 240) ?? null,
        audio:
          document
            .querySelector('[data-h3-nle-status="audio"]')
            ?.getAttribute("data-h3-nle-audio-state") ?? null,
      }))
      .catch(() => null);
  } finally {
    const cleanupErrors: unknown[] = [];
    const stage = async (name: string, operation: () => Promise<unknown>) => {
      try {
        cleanup[name] = { status: "pass", result: await operation() };
      } catch (error) {
        cleanup[name] = {
          status: "fail",
          errorType: error instanceof Error ? error.name : "unknown",
        };
        cleanupErrors.push(error);
      }
    };
    // Independent stages deliberately continue after failure. In particular, a failed
    // borrowed-source release must never leave the passive owner observer installed.
    await stage("closeOverlay", async () => {
      const close = page.locator(
        '[data-h3-nle-surface="overlay_v1"] [data-h3-nle-action="close"]',
      );
      if (await close.isVisible()) await close.click();
      await Promise.all(responseTasks);
      return "closed_or_absent";
    });
    await stage("managedParent", async () => {
      if (!managedProducerStarted) return "not_started";
      const snapshot = await page.evaluate(
        async ({ bundleUrl, priorParentId }) => {
          const module = await import(/* @vite-ignore */ bundleUrl);
          const parent = module.managedProductionSession;
          const before = parent.snapshot();
          if (
            before.parentSequenceId &&
            before.parentSequenceId !== priorParentId &&
            !["succeeded", "cancelled"].includes(before.parentState)
          )
            await parent.cancel();
          await parent.settle();
          return parent.snapshot();
        },
        { bundleUrl: candidateBundleResourceUrl(hostUrl!), priorParentId },
      );
      if (snapshot.parentSequenceId)
        expect(["succeeded", "cancelled"]).toContain(snapshot.parentState);
      await expect
        .poll(() => supportedHostQueueCounts(page), { timeout: 60_000 })
        .toEqual({ running: 0, pending: 0 });
      return snapshot;
    });
    await stage("renderJob", async () => {
      await Promise.all(responseTasks);
      const job = [...outputStatuses]
        .reverse()
        .find((row) => row.workspace_handle === authoringHandle);
      if (!job || ["succeeded", "failed", "cancelled"].includes(job.phase))
        return "terminal_or_absent";
      const cancelled = await post(
        page,
        `${OUTPUT_CAPABILITY.job_path}/${job.job_handle}/cancel`,
        {
          schema: "h3.authoring.output_cancel.v1",
          workspace_handle: job.workspace_handle,
        },
      );
      expect(cancelled.status).toBe(200);
      await expect
        .poll(
          async () => {
            const result = await page.evaluate(
              async (path) => {
                const response = await fetch(path, {
                  credentials: "same-origin",
                });
                return { status: response.status, body: await response.json() };
              },
              `${OUTPUT_CAPABILITY.job_path}/${job.job_handle}?workspace_handle=${encodeURIComponent(job.workspace_handle)}`,
            );
            expect(result.status).toBe(200);
            return ["succeeded", "failed", "cancelled"].includes(
              decodeOutputStatus(result.body).phase,
            );
          },
          { timeout: 60_000 },
        )
        .toBe(true);
      return "terminal";
    });
    await stage("assemblyJob", async () => {
      if (!productionHandle) return "not_created";
      const current = await readProduction(productionHandle);
      if (current.allowedActions.includes("cancel_assembly")) {
        const cancelled = await post(
          page,
          route,
          encodeProductionAction(
            requestId("assembly.cancel"),
            "cancel_assembly",
            { projection: current },
          ),
        );
        expect(cancelled.status).toBe(200);
      }
      await expect
        .poll(
          async () =>
            ["running", "cancelling"].includes(
              (await readProduction(productionHandle!)).assembly.state,
            ),
          { timeout: 60_000 },
        )
        .toBe(false);
      return "terminal_or_absent";
    });
    await stage("authoringRelease", async () => {
      if (!authoringHandle) return "not_created";
      const result = await post(
        page,
        "/h3-context/v1/authoring/action",
        encodeAuthoringAction(
          requestId("authoring.release"),
          "release_workspace",
          { workspace_handle: authoringHandle },
        ),
      );
      expect(result.status).toBe(204);
      return result.status;
    });
    await stage("productionRelease", async () => {
      if (!productionHandle) return "not_created";
      const fresh = await readProduction(productionHandle);
      const result = await post(
        page,
        route,
        encodeProductionAction(requestId("release"), "release_workspace", {
          projection: fresh,
        }),
      );
      expect(result.status).toBe(204);
      return result.status;
    });
    await stage("observerStop", async () => {
      const stopped = await ownerCapture("stop");
      expect(stopped.capture.observerRestored).toBe(true);
      if (!primaryFailure && observationStarted)
        expect(stopped.capture.workerSubmissions).toBe(1);
      return stopped.capture;
    });
    page.off("request", onRequest);
    page.off("response", onResponse);
    await Promise.all(responseTasks);
    cleanupErrors.push(...responseErrors);
    observations.cleanup = cleanup;
    observations.promptPosts = promptPosts;
    observations.productionWrites = productionWrites;
    observations.productionResponses = productionResponses;
    observations.runtimeResponses = runtimeResponses;
    observations.leaseResponses = leaseResponses;
    observations.importResponses = imports;
    observations.authoringResponses = authoringResponses;
    observations.shellDiagnostics = await page
      .evaluate(() => {
        const runtime = window as unknown as Record<string, unknown>;
        const stages = (name: string) =>
          Array.isArray(runtime[name]) ? runtime[name].slice(-64) : [];
        return {
          status: Array.from(
            document.querySelectorAll("[data-shell-status]"),
          ).map((element) => ({
            status: element.getAttribute("data-shell-status"),
            reason: element.getAttribute("data-shell-reason"),
          })),
          projectionTrace: stages("__h3ProjectionTrace"),
          hostProjectionTrace: stages("__h3HostProjectionTrace"),
          producer: runtime.__h3StockRuntimeProducerDiagnostic ?? null,
        };
      })
      .catch(() => ({ unavailable: true }));
    observations.outcome = {
      main: primaryFailure ? "fail" : "pass",
      cleanup: cleanupErrors.length ? "fail" : "pass",
    };
    await stage("evidenceWrite", async () => {
      await writeFile(
        resolve(evidence, "runtime-observations.json"),
        JSON.stringify(observations, null, 2),
      );
    });
    await stage("evidenceAttachment", async () => {
      await testInfo.attach("production-runtime-integration", {
        body: JSON.stringify(observations),
        contentType: "application/json",
      });
    });
    if (primaryFailure || cleanupErrors.length)
      throw new AggregateError(
        [...(primaryFailure ? [primaryFailure] : []), ...cleanupErrors],
        "runtime integration or owned cleanup failed",
      );
  }
  expect(cleanup.authoringRelease).toEqual({ status: "pass", result: 204 });
  expect(cleanup.productionRelease).toEqual({ status: "pass", result: 204 });
});
