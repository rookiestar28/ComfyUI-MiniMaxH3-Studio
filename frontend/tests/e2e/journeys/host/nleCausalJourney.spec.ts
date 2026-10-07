import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import type { Download, Request, Response } from "@playwright/test";

import {
  decodeProductionWorkbenchProjection,
  encodeProductionAction,
} from "../../../../src/contracts/productionWorkbenchCodec";
import { decodeProductionAuthoringImportResponseV1 as decodeProductionAuthoringImportResponse } from "../../../../src/contracts/productionAuthoringImportCodec";
import {
  decodeTimelineHistoryProjection,
  decodeTimelineReceipt,
  encodeAuthoringAction,
  type TimelineReceipt,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import {
  decodeOutputStatus,
  OUTPUT_CAPABILITY,
} from "../../../../src/contracts/authoringOutputCodec";
import {
  AUTHORING_MEDIA_LEASE_OPEN_ROUTE,
  AUTHORING_MEDIA_LEASE_ROUTE,
} from "../../../../src/host/authoringMediaSourceLease";
import { observeOwnedGraph } from "../../../../src/host/ownedGraphIdentity";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import { openExportPanel } from "../../helpers/nleExport";
import { startProcessAudioObserver } from "../../host/audioObserver";
import { normalizeM2508HostApiPath } from "../../host/m25_08RequestClassification";
import {
  ensureM2508CapturedWorkflowAuthority,
  materializeM2508VisiblePromptInCapturedWorkflow,
} from "../../host/m25_08Bootstrap";
import {
  installProductionRuntimeProducer,
  type ProductionRuntimeProducerHandle,
  type ProductionRuntimeProducerResult,
} from "../../host/productionRuntimeProducer";
import {
  createOwnerCapture,
  ownedPath,
  post,
  readReadyPreviewBytes,
} from "../../host/productionRuntimeRow";
import { expect, test, type Page } from "../../host/fixture";
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
  setSupportedH3Language,
  supportedHostQueueCounts,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "../../host/environment";

type CausalJourneyFixture = {
  schema: "NleCausalJourneyFixtureV1";
  inputFilenames: string[];
  runPrefix: string;
  evidenceRelative: string;
  apparatusRelative: string;
  audioObserver: {
    observerRelative: string;
    observerSha256: string;
    browserExecutableSha256: string;
  };
};

const PRODUCTION_ROUTE = "/h3-context/v1/production/action";
const AUTHORING_ROUTE = "/h3-context/v1/authoring/action";
const MANAGED_ROUTE = "/h3-context/v1/managed-sequences";
const COORDINATOR_ROUTE = "/h3-context/v1/generation/coordinator";
const IMPORT_ROUTE = "/h3-context/v1/production/authoring-import";
const CONTAINER_ID = "h3-context-nle-causal-journey";

// Real process-loopback audio needs the test browser's own output; the lane never opens a
// microphone and retains only bounded packet statistics.
test.use({ launchOptions: { ignoreDefaultArgs: ["--mute-audio"] } });

type AwayView = Readonly<{ kind: "host_tab_destroy_then_render" }>;

/** Leave the H3 view through the host tab's own destroy, exactly as a sidebar switch unmounts it. */
async function leaveH3View(page: Page, containerId: string): Promise<AwayView> {
  await page.evaluate((id) => {
    const app = (window as unknown as { comfyAPI: { app: { app: any } } })
      .comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((candidate: { id: string }) => candidate.id === "h3-context");
    if (tab === undefined || typeof tab.destroy !== "function")
      throw new Error("H3 custom tab destroy is absent");
    tab.destroy();
    document.getElementById(id)?.closest(".side-bar-panel")?.remove();
  }, containerId);
  await expect(page.locator(`#${containerId}`)).toHaveCount(0);
  await expect(page.locator('[data-h3-nle-surface="overlay_v1"]')).toHaveCount(
    0,
  );
  return { kind: "host_tab_destroy_then_render" };
}

/** Return to a freshly rendered H3 view; nothing from the destroyed view is reused. */
async function returnH3View(page: Page, containerId: string, away: AwayView) {
  expect(away.kind).toBe("host_tab_destroy_then_render");
  return openH3AppModeTab(page, containerId);
}

// M25-22 T1: the single formal four-child causal interval, from real source context through
// UI planning, the product's own Start and B1 leave/return, explicit assembly, a proper-subset
// original import, separate insertion, real edits and transport, render, preview and download.
// T2 then retains that view across native Sidebar collapse/expand and tab leave/return, and T3
// releases the Production owner of the imported source. T1's counters close before T2 starts.
// The explicit opt-in keeps an ordinary host run from touching a supplied host or private apparatus.
test("four real children flow from UI planning through B1, assembly, import, editing and download", async ({
  page,
  context,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_NLE_CAUSAL_JOURNEY !== "1",
    "explicit supplied-host causal journey required",
  );
  // The plan's retained abort ceilings summed (T1 90, T2 20, T3 20 minutes); not expected durations.
  test.setTimeout(130 * 60_000);
  if (!hostUrl || !candidateBundle || candidateBackendMode !== "exact")
    throw new Error(
      "an exact installed candidate and supplied host are required",
    );
  const fixtureValue = process.env.H3_CONTEXT_NLE_CAUSAL_FIXTURE;
  if (!fixtureValue) throw new Error("causal journey fixture is required");
  const fixture: CausalJourneyFixture = JSON.parse(
    await readFile(ownedPath(fixtureValue), "utf8"),
  );
  if (
    fixture.schema !== "NleCausalJourneyFixtureV1" ||
    fixture.inputFilenames.length !== 4 ||
    new Set(fixture.inputFilenames).size !== 4 ||
    !fixture.inputFilenames.every((name) =>
      /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mp4$/.test(name),
    ) ||
    !/^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$/.test(fixture.runPrefix) ||
    !/^[a-f0-9]{64}$/.test(fixture.audioObserver?.observerSha256 ?? "") ||
    !/^[a-f0-9]{64}$/.test(fixture.audioObserver?.browserExecutableSha256 ?? "")
  )
    throw new Error("invalid four-child causal journey fixture");
  const evidence = ownedPath(fixture.evidenceRelative);
  const apparatus = ownedPath(fixture.apparatusRelative);
  await mkdir(evidence, { recursive: true });
  const ownerCapture = await createOwnerCapture(apparatus, evidence);
  const bundleUrl = candidateBundleResourceUrl(hostUrl);
  let serial = 0;
  const requestId = (name: string) =>
    `${fixture.runPrefix}.${name}.${++serial}`;
  const readProduction = async (handle: string) => {
    const response = await post(
      page,
      PRODUCTION_ROUTE,
      encodeProductionAction(requestId("read"), "read_projection", {
        workspaceHandle: handle,
      }),
    );
    expect(response.status, JSON.stringify(response.body)).toBe(200);
    return decodeProductionWorkbenchProjection(response.body);
  };

  // ---------------------------------------------------------------- owned effect counters
  let productionHandle: string | undefined;
  let authoringHandle: string | undefined;
  let observationStarted = false;
  let producer: ProductionRuntimeProducerHandle | undefined;
  let producerStarted = false;
  let priorParentId: string | null = null;
  let audioObserver:
    Awaited<ReturnType<typeof startProcessAudioObserver>> | undefined;
  let primaryFailure: unknown;
  let stage = "setup";
  const responseTasks: Promise<void>[] = [];
  const responseErrors: unknown[] = [];
  const counters = {
    promptPosts: 0,
    productionActions: [] as string[],
    managedActions: [] as string[],
    authoringActions: [] as string[],
    imports: 0,
    outputCreates: 0,
    outputPreviews: 0,
    outputDownloads: 0,
    ownedRequests: 0,
  };
  const snapshotCounters = () => ({
    promptPosts: counters.promptPosts,
    productionActions: counters.productionActions.length,
    managedActions: counters.managedActions.length,
    authoringActions: counters.authoringActions.length,
    imports: counters.imports,
    outputCreates: counters.outputCreates,
    outputPreviews: counters.outputPreviews,
    outputDownloads: counters.outputDownloads,
    ownedRequests: counters.ownedRequests,
  });
  const phaseMarks: Record<
    string,
    ReturnType<typeof snapshotCounters> & { elapsedMs: number }
  > = {};
  const journeyStartedAt = Date.now();
  const mark = (name: string) => {
    phaseMarks[name] = {
      ...snapshotCounters(),
      elapsedMs: Date.now() - journeyStartedAt,
    };
  };
  // Plan section 15.3 ceilings per phase; one test holds all three, so each is checked at its mark.
  const withinCeiling = (from: string | null, to: string, minutes: number) => {
    const start = from === null ? 0 : phaseMarks[from]!.elapsedMs;
    expect(
      phaseMarks[to]!.elapsedMs - start,
      `${to} ceiling ${minutes} min`,
    ).toBeLessThanOrEqual(minutes * 60_000);
  };
  const imports: Array<{ request: any; status: number; body: any }> = [];
  const authoringResponses: Array<{
    action: string;
    status: number;
    body: any;
  }> = [];
  const receipts: TimelineReceipt[] = [];
  const outputStatuses: ReturnType<typeof decodeOutputStatus>[] = [];
  const productionResponses: Array<{
    action: string;
    status: number;
    body: any;
  }> = [];
  const runtimeResponses: Array<Record<string, unknown>> = [];
  // Bounded dispositions only: never a lease capability, body or output bytes.
  const leaseResponses: Array<{
    operation: string;
    status: number;
    reason: string | null;
  }> = [];
  const outputCreateResponses: Array<{
    status: number;
    code: string | null;
    phase: string | null;
    jobHandle: string | null;
  }> = [];
  // Failure diagnosis for owned routes and the candidate page: bounded, with opaque path segments
  // and any URL or long token redacted, so a stage failure names its seam without a rerun.
  const redact = (text: string, limit: number) =>
    text
      .replace(/\b(?:https?|blob|data):\S+/g, "<url>")
      .replace(/[A-Za-z0-9_-]{24,}/g, "<token>")
      .slice(0, limit);
  const ownedRoute = (path: string) =>
    path
      .split("/")
      .map((part) => (/^[A-Za-z0-9_-]{16,}$/.test(part) ? ":id" : part))
      .join("/");
  const ownedFailures: Array<{
    at: number;
    stage: string;
    method: string;
    route: string;
    status: number | null;
    failure: string | null;
  }> = [];
  const pageErrors: Array<{ at: number; stage: string; message: string }> = [];
  // Repository-owned monitor status copy with the journey stage at each change, so a monitor that
  // becomes unavailable without a lease refusal can be placed against the step that preceded it.
  const monitorTimeline: Array<{ at: number; stage: string; status: string }> =
    [];
  let productionReleasedByT3 = false;
  const cleanup: Record<string, unknown> = {};
  const observations: Record<string, unknown> = {
    schema: "NleCausalJourneyObservationsV1",
    candidateBundleSha256: candidateBundle.sha256,
    phaseMarks,
  };
  const onRequest = (request: Request) => {
    const path = normalizeM2508HostApiPath(new URL(request.url()).pathname);
    const method = request.method();
    if (path.startsWith("/h3-context/")) counters.ownedRequests++;
    if (method === "POST" && path === "/prompt") counters.promptPosts++;
    if (method === "POST" && path === PRODUCTION_ROUTE)
      counters.productionActions.push(String(request.postDataJSON()?.action));
    if (method === "POST" && path === MANAGED_ROUTE)
      counters.managedActions.push(String(request.postDataJSON()?.action));
    if (method === "POST" && path === AUTHORING_ROUTE)
      counters.authoringActions.push(String(request.postDataJSON()?.action));
    if (method === "POST" && path === IMPORT_ROUTE) counters.imports++;
    if (method === "POST" && path === OUTPUT_CAPABILITY.job_path)
      counters.outputCreates++;
    if (
      method === "GET" &&
      path.startsWith(`${OUTPUT_CAPABILITY.output_path}/`)
    ) {
      if (path.endsWith("/preview")) counters.outputPreviews++;
    }
  };
  // IMPORTANT (B-M2522-SPEC-03): an `<a download>` request goes to the browser's download
  // manager and never raises a page `request` event, so a request-path counter reads 0 after a
  // real download. Count the browser's own download events for the owned output route instead.
  const onDownload = (download: Download) => {
    const path = normalizeM2508HostApiPath(new URL(download.url()).pathname);
    if (
      path.startsWith(`${OUTPUT_CAPABILITY.output_path}/`) &&
      path.endsWith("/download")
    )
      counters.outputDownloads++;
  };
  const onRequestFailed = (request: Request) => {
    const path = normalizeM2508HostApiPath(new URL(request.url()).pathname);
    if (path.startsWith("/h3-context/") && ownedFailures.length < 64)
      ownedFailures.push({
        at: Date.now() - journeyStartedAt,
        stage,
        method: request.method(),
        route: ownedRoute(path),
        status: null,
        failure: redact(request.failure()?.errorText ?? "", 80),
      });
  };
  const onPageError = (error: Error) => {
    if (pageErrors.length < 16)
      pageErrors.push({
        at: Date.now() - journeyStartedAt,
        stage,
        message: redact(`${error.name}: ${error.message}`, 160),
      });
  };
  const onResponse = (response: Response) => {
    const path = normalizeM2508HostApiPath(new URL(response.url()).pathname);
    const method = response.request().method();
    if (
      path.startsWith("/h3-context/") &&
      response.status() >= 400 &&
      ownedFailures.length < 64
    )
      ownedFailures.push({
        at: Date.now() - journeyStartedAt,
        stage,
        method,
        route: ownedRoute(path),
        status: response.status(),
        failure: null,
      });
    if (
      method === "POST" &&
      (path === AUTHORING_MEDIA_LEASE_ROUTE ||
        path === AUTHORING_MEDIA_LEASE_OPEN_ROUTE)
    ) {
      responseTasks.push(
        (async () => {
          const status = response.status();
          const operation = String(
            response.request().postDataJSON()?.operation ?? "unknown",
          ).slice(0, 40);
          // A seam-level refusal can be bodiless; its status alone is the disposition.
          const reason =
            status >= 400
              ? String(
                  (await response.json().catch(() => null))?.reason ?? "",
                ).slice(0, 60)
              : null;
          leaseResponses.push({ operation, status, reason });
        })().catch((error: unknown) => {
          responseErrors.push(error);
        }),
      );
      return;
    }
    if (method === "POST" && path === OUTPUT_CAPABILITY.job_path)
      responseTasks.push(
        (async () => {
          const status = response.status();
          const body = await response.json().catch(() => null);
          outputCreateResponses.push({
            status,
            code: status >= 400 ? String(body?.code ?? "").slice(0, 60) : null,
            phase: status < 400 ? String(body?.phase ?? "") : null,
            jobHandle: status < 400 ? String(body?.job_handle ?? "") : null,
          });
        })().catch((error: unknown) => {
          responseErrors.push(error);
        }),
      );
    if (
      ![
        PRODUCTION_ROUTE,
        MANAGED_ROUTE,
        COORDINATOR_ROUTE,
        IMPORT_ROUTE,
        AUTHORING_ROUTE,
      ].includes(path) &&
      !(
        method === "GET" &&
        (path === OUTPUT_CAPABILITY.job_path ||
          path.startsWith(`${OUTPUT_CAPABILITY.job_path}/`))
      ) &&
      !(method === "POST" && path === OUTPUT_CAPABILITY.job_path)
    )
      return;
    // IMPORTANT (B-M2522-SPEC-05): an owned refusal can be bodiless (a released Production
    // workspace reads 410 with no body). Parse only success bodies strictly; a refusal keeps its
    // status with a null body, or cleanup reports a JSON error for a correct refusal.
    const bodyOf = async (): Promise<any> => {
      if (response.status() === 204) return null;
      if (response.ok()) return response.json();
      return response.json().catch(() => null);
    };
    responseTasks.push(
      (async () => {
        if (path === MANAGED_ROUTE || path === COORDINATOR_ROUTE) {
          const body = (await bodyOf()) ?? {};
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
          // A child failed before its queue reaches the host as one hashed phase. Keep the
          // runner's failure fingerprint, the prepared join and the workflow store, all
          // content-free, so a failed start can be localized after the run.
          const entry = runtimeResponses[runtimeResponses.length - 1];
          if (request?.action === "fail_prepared_child") {
            const failure =
              request?.payload?.failure_fingerprint ??
              request?.failure_fingerprint;
            entry.failureFingerprint =
              typeof failure === "string" &&
              /^[A-Za-z0-9:]{1,80}$/.test(failure)
                ? failure
                : null;
            entry.workflowStore = await page
              .evaluate(() => {
                const w = window as any;
                const app = w.app ?? w.comfyAPI?.app?.app;
                const store = app?.extensionManager?.workflow;
                const active = store?.activeWorkflow;
                const open = store?.openWorkflows;
                let serialize = "absent";
                try {
                  if (typeof app?.graph?.serialize === "function")
                    serialize = app.graph.serialize() ? "ready" : "empty";
                } catch {
                  serialize = "threw";
                }
                return {
                  hasApp: Boolean(app),
                  hasStore: Boolean(store),
                  activeIsObject:
                    active !== null &&
                    typeof active === "object" &&
                    !Array.isArray(active),
                  openIsArray: Array.isArray(open),
                  openCount: Array.isArray(open) ? open.length : null,
                  openIncludesActive: Array.isArray(open)
                    ? open.includes(active)
                    : null,
                  serialize,
                };
              })
              .catch(() => null);
          }
          if (request?.action === "prepare_sequence_child") {
            const execution = body.execution;
            entry.preparedJoin =
              execution !== null && typeof execution === "object"
                ? {
                    segmentMatches:
                      execution.segment_id === request?.payload?.segment_id,
                    parentMatches:
                      execution.parent_sequence_id === body.parent_sequence_id,
                    authorizationMatches:
                      execution.parent_authorization_fingerprint ===
                      body.authorization_fingerprint,
                    slotRevisionMatches:
                      execution.slot_revision === body.revision,
                    predecessorNull:
                      execution.predecessor_terminal_fingerprint === null,
                    hasContextHandle:
                      typeof body.context_workspace_handle === "string",
                  }
                : { hasExecution: false };
          }
        } else if (path === PRODUCTION_ROUTE) {
          const action = String(response.request().postDataJSON()?.action);
          const body = await bodyOf();
          // IMPORTANT: learn cleanup ownership from the actual Shell response even if the
          // next assertion fails. A late waiter cannot prove that no workspace was created.
          if (
            action === "create_workspace_from_context" &&
            response.status() === 201
          )
            productionHandle =
              decodeProductionWorkbenchProjection(body).workspaceHandle;
          productionResponses.push({ action, status: response.status(), body });
        } else if (path === IMPORT_ROUTE) {
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
        } else if (path === AUTHORING_ROUTE) {
          const action = String(response.request().postDataJSON()?.action);
          const body = await bodyOf();
          if (
            action === "create_authoring_workspace" &&
            response.status() === 201
          )
            authoringHandle = body.workspace_handle;
          if (action === "apply_timeline_transaction" && response.ok())
            receipts.push(decodeTimelineReceipt(body));
          authoringResponses.push({ action, status: response.status(), body });
        } else if (response.ok())
          outputStatuses.push(decodeOutputStatus(await response.json()));
      })().catch((error: unknown) => {
        responseErrors.push(error);
      }),
    );
  };
  page.on("request", onRequest);
  page.on("response", onResponse);
  page.on("requestfailed", onRequestFailed);
  page.on("pageerror", onPageError);
  page.on("download", onDownload);
  await page.exposeFunction("__h3M2522MonitorStatus", (status: unknown) => {
    if (typeof status === "string" && monitorTimeline.length < 96)
      monitorTimeline.push({
        at: Date.now() - journeyStartedAt,
        stage,
        status: status.slice(0, 80),
      });
  });
  // Installed for each document the page loads; the host page is navigated after this point.
  await page.addInitScript(() => {
    if (window.top !== window) return;
    let last: string | null = null;
    const read = () => {
      const status =
        document
          .querySelector('[data-h3-nle-status="monitor"]')
          ?.textContent?.slice(0, 80) ?? "absent";
      if (status === last) return;
      last = status;
      void (
        window as unknown as { __h3M2522MonitorStatus(value: string): void }
      ).__h3M2522MonitorStatus(status);
    };
    new MutationObserver(read).observe(document, {
      subtree: true,
      childList: true,
      characterData: true,
    });
    read();
  });
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  const sequencePane = overlay.locator('[data-h3-nle-region="sequence"]');
  const sequenceStatus = sequencePane.locator(
    '[data-h3-nle-status="sequence"]',
  );
  let shell = page.locator(`#${CONTAINER_ID}`);
  const launcher = () => shell.locator('[data-h3-nle-entry="open"]');
  const openOverlay = async (pane: "sequence" | "inspector" | "assets") => {
    await shell.locator('[data-h3-director-function="clip_editor"]').click();
    await launcher().click();
    await expect(overlay).toHaveCount(1);
    await expect(overlay.locator("h2")).toBeFocused();
    const tab = overlay.locator(`[data-h3-nle-pane="${pane}"]`);
    if ((await tab.count()) > 0) await tab.first().click();
  };
  const closeOverlay = async () => {
    await overlay.locator('[data-h3-nle-action="close"]').click();
    await expect(overlay).toHaveCount(0);
    await expect(launcher()).toBeFocused();
  };
  const activate = async (control: string) => {
    const button = overlay.locator(`[data-h3-nle-control="${control}"]`);
    await expect(button).toBeEnabled({ timeout: 60_000 });
    await button.click();
  };
  const hostQueueIdle = async () =>
    expect(await supportedHostQueueCounts(page)).toEqual({
      running: 0,
      pending: 0,
    });

  try {
    // -------------------------------------------------------------- declared setup
    await hostQueueIdle();
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
    shell = await openH3AppModeTab(page, CONTAINER_ID);
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
    const bootstrapPrompt = await page.evaluate(async (bootstrap) => {
      const runtime = window as unknown as {
        comfyAPI: { api: { api: any } };
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
    await waitForHostGraphSettled(page);
    await shell.locator('[data-page-id="production"]').click();
    await expect.poll(() => productionHandle, { timeout: 30_000 }).toBeTruthy();
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
    expect(counters.promptPosts).toBe(1);
    observations.setup = {
      contextSetupPromptId: bootstrapPrompt,
      contextWorkspaceHandle: source!.workspace_id,
      promptPosts: counters.promptPosts,
    };
    priorParentId = await page.evaluate(async (bundleUrl) => {
      const module = await import(/* @vite-ignore */ bundleUrl);
      const snapshot = module.managedProductionSession.snapshot();
      if (
        snapshot.parentSequenceId &&
        !["succeeded", "cancelled"].includes(snapshot.parentState)
      )
        throw new Error("causal journey cannot adopt an active managed parent");
      return snapshot.parentSequenceId;
    }, bundleUrl);
    mark("setup");

    // -------------------------------------------------------------- T1 planning (UI only)
    stage = "planning";
    // M25-63: planning lives in the sidebar's Production tab (entered in setup above). The editor's
    // Sequence tab keeps the readiness request and every run control, so readiness and Start are
    // driven there. The page restores its last function tab, so the workbench is chosen explicitly.
    await shell
      .locator('[data-h3-director-function="production_workbench"]')
      .click();
    const planningSection = shell.locator("[data-h3-nle-planning-status]");
    await expect(planningSection).toBeVisible({ timeout: 30_000 });
    await planningSection
      .locator('[data-h3-nle-control="planning.target_seconds"]')
      .fill("60");
    await planningSection
      .locator('[data-h3-nle-control="planning.policy"]')
      .selectOption("fixed_15");
    for (const [control, status] of [
      ["planning.prepare_context", "prepared"],
      ["planning.admit_canonical", "admitted"],
      ["planning.propose", "proposed"],
      ["planning.approve_import", "imported"],
    ] as const) {
      const button = planningSection.locator(
        `[data-h3-nle-control="${control}"]`,
      );
      await expect(button).toBeEnabled({ timeout: 60_000 });
      await button.click();
      await expect(planningSection).toHaveAttribute(
        "data-h3-nle-planning-status",
        status,
        { timeout: 60_000 },
      );
    }
    await expect(
      planningSection.locator(
        '[data-h3-nle-region="proposal"] [data-h3-nle-segment]',
      ),
    ).toHaveCount(4);
    await expect(
      planningSection.locator('[data-h3-nle-status="plan"]'),
    ).toBeVisible();
    await openOverlay("sequence");
    await expect(sequencePane).toBeVisible();
    await expect(
      sequencePane.locator('[data-h3-nle-control^="planning."]'),
    ).toHaveCount(0);
    await activate("readiness.request");
    await expect(
      sequencePane.locator('[data-h3-nle-status="readiness"]'),
    ).toHaveAttribute("data-state", "ready", { timeout: 60_000 });
    let current = await readProduction(productionHandle!);
    const segmentIds = [...current.segments]
      .sort((left, right) => left.ordinal - right.ordinal)
      .map((segment) => segment.segmentId);
    expect(segmentIds).toHaveLength(4);
    expect(
      current.segments.map((segment) => segment.duration.requestedMilliseconds),
    ).toEqual([15_000, 15_000, 15_000, 15_000]);
    // Planning, review/import and readiness are explicit actions with zero queue effects.
    expect(counters.promptPosts).toBe(1);
    await hostQueueIdle();
    expect(
      counters.managedActions.filter((action) =>
        [
          "authorize_sequence",
          "start_sequence",
          "prepare_sequence_child",
        ].includes(action),
      ),
    ).toEqual([]);
    expect(counters.productionActions).not.toContain("assemble_sequence");
    observations.planning = {
      segmentIds,
      durations: current.segments.map((segment) => segment.duration),
      productionActions: [...counters.productionActions],
    };
    mark("planned");

    // -------------------------------------------------------------- T1 explicit Start and B1
    stage = "start";
    producer = await installProductionRuntimeProducer(page, {
      bundleUrl,
      workspaceHandle: productionHandle!,
      segmentIds,
      ownedReference,
      inputFilename: fixture.inputFilenames,
      runPrefix: fixture.runPrefix,
      timeoutMs: 900_000,
      start: "native",
    });
    // Installation authorizes, starts and queues nothing.
    expect(counters.promptPosts).toBe(1);
    expect((await producer.progress()).queued).toEqual([]);
    producerStarted = true;
    await activate("sequence.start");
    await expect(sequenceStatus).toHaveAttribute("data-state", "detach_ready", {
      timeout: 180_000,
    });
    stage = "b1_detach";
    await activate("sequence.detach");
    await expect(sequenceStatus).toHaveAttribute(
      "data-state",
      "safe_to_leave",
      {
        timeout: 60_000,
      },
    );
    const atDetach = await producer.progress();
    // B1 must leave at least one successor for the explicit resume to own.
    expect(atDetach.queued.length).toBeGreaterThanOrEqual(1);
    expect(atDetach.queued.length).toBeLessThan(4);
    await closeOverlay();
    const away = await leaveH3View(page, CONTAINER_ID);
    // The current child still completes and is captured without its view; no successor starts.
    await expect
      .poll(async () => (await producer!.progress()).executedPromptIds.length, {
        timeout: 300_000,
      })
      .toBe(atDetach.queued.length);
    await expect
      .poll(
        () =>
          page.evaluate(async (bundleUrl) => {
            const module = await import(/* @vite-ignore */ bundleUrl);
            const snapshot = module.managedProductionSession.snapshot();
            return {
              parentState: snapshot.parentState,
              activeSegmentId: snapshot.activeSegmentId,
            };
          }, bundleUrl),
        { timeout: 120_000 },
      )
      .toEqual({ parentState: "paused_client_absent", activeSegmentId: null });
    // Hold across a fixed observation window: a detached parent never submits a successor.
    await page.waitForTimeout(5_000);
    expect((await producer.progress()).queued).toHaveLength(
      atDetach.queued.length,
    );
    expect(counters.promptPosts).toBe(1 + atDetach.queued.length);
    await hostQueueIdle();
    stage = "b1_return";
    shell = await returnH3View(page, CONTAINER_ID, away);
    await shell.locator('[data-page-id="production"]').click();
    await openOverlay("sequence");
    await activate("sequence.reattach");
    await expect(sequenceStatus).toHaveAttribute("data-state", "resume_ready", {
      timeout: 60_000,
    });
    expect((await producer.progress()).queued).toHaveLength(
      atDetach.queued.length,
    );
    expect(counters.promptPosts).toBe(1 + atDetach.queued.length);
    observations.b1 = {
      queuedAtDetach: atDetach.queued.length,
      leave: away.kind,
      heldPromptPosts: counters.promptPosts,
    };
    mark("b1_returned");
    stage = "resume";
    await activate("sequence.resume");
    const produced: ProductionRuntimeProducerResult =
      await producer.collect(900_000);
    await expect(sequenceStatus).toHaveAttribute("data-state", "finished", {
      timeout: 60_000,
    });
    await producer.dispose();
    observations.producer = produced;
    expect(produced.childPromptIds).toHaveLength(4);
    expect(new Set(produced.childPromptIds).size).toBe(4);
    for (const [index, proof] of produced.sinkExecutionProofs.entries()) {
      expect(proof.promptId).toBe(produced.childPromptIds[index]);
      expect(proof.executingSequence).toBeLessThan(proof.executedSequence);
      if (index > 0)
        expect(
          produced.sinkExecutionProofs[index - 1]!.executedSequence,
        ).toBeLessThan(proof.executingSequence);
    }
    expect(produced.captureReceipts).toHaveLength(4);
    expect(counters.promptPosts).toBe(5);
    await hostQueueIdle();
    mark("children_succeeded");

    // -------------------------------------------------------------- T1 explicit assembly
    stage = "assembly";
    current = await readProduction(productionHandle!);
    expect(current.runState).toBe("succeeded");
    const originals = current.outputs.filter(
      (output) => output.segmentId !== null,
    );
    expect(originals.filter((output) => output.state === "ready")).toHaveLength(
      4,
    );
    expect(current.assembly.state).toBe("unavailable");
    expect(current.allowedActions).toContain("assemble_sequence");
    expect(counters.productionActions).not.toContain("assemble_sequence");
    expect(counters.imports).toBe(0);
    // Even an uncertain observe response may have installed the wrapper; always send stop.
    observationStarted = true;
    const observation = await ownerCapture("observe", current);
    expect(observation.capture).toEqual({
      observerInstalled: true,
      workerSubmissions: 0,
    });
    await activate("assembly.assemble");
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
        { timeout: 300_000, intervals: [500, 1000, 2000] },
      )
      .toBe("succeeded");
    const assembled = current;
    expect(
      counters.productionActions.filter(
        (action) => action === "assemble_sequence",
      ),
    ).toHaveLength(1);
    await activate("production.refresh");
    await expect(
      sequencePane.locator("[data-h3-nle-assembly-state]"),
    ).toHaveAttribute("data-h3-nle-assembly-state", "succeeded");
    expect(
      assembled.outputs.filter((output) => output.segmentId !== null),
    ).toEqual(originals);
    const aggregate = assembled.outputs.find(
      (output) => output.segmentId === null,
    );
    // A valid 60-second aggregate is ready but beyond the 30-second public preview cap.
    expect(aggregate).toMatchObject({ state: "ready", preview: false });
    const captured = await ownerCapture("capture", assembled);
    expect(captured.capture.workerSubmissions).toBe(1);
    expect(captured.capture.persistentOutputInspection).toBe("complete");
    expect(
      captured.capture.originalArtifactReceipts
        .map((row: any) => row.receipt_fingerprint)
        .sort(),
    ).toEqual(
      produced.captureReceipts.map((row) => row.receiptFingerprint).sort(),
    );
    expect(
      decodeProductionWorkbenchProjection(captured.capture.projection),
    ).toEqual(assembled);
    expect(counters.imports).toBe(0);
    expect(counters.promptPosts).toBe(5);
    observations.assembly = { projection: assembled, capture: captured };
    mark("assembled");

    // -------------------------------------------------------------- T1 proper-subset import
    stage = "selection";
    await closeOverlay();
    await shell
      .locator('[data-h3-director-function="production_workbench"]')
      .click();
    const selectedIds = originals.slice(0, 3).map((row) => row.segmentId!);
    const segmentRows = shell.locator("ol.h3p-s > li");
    await expect(segmentRows).toHaveCount(4);
    for (const [index, segmentId] of segmentIds.entries()) {
      const checkbox = segmentRows.nth(index).locator('input[type="checkbox"]');
      const wanted = selectedIds.includes(segmentId);
      await expect(checkbox).toBeEnabled();
      if ((await checkbox.isChecked()) === wanted) continue;
      const selected = page.waitForResponse(
        (response) =>
          normalizeM2508HostApiPath(new URL(response.url()).pathname) ===
            PRODUCTION_ROUTE &&
          response.request().postDataJSON()?.action === "set_selection",
      );
      await checkbox.click();
      expect((await selected).status()).toBe(200);
      await expect(checkbox).toBeChecked({ checked: wanted });
    }
    current = await readProduction(productionHandle!);
    expect([...current.selectedSegmentIds].sort()).toEqual(
      [...selectedIds].sort(),
    );
    expect(current.assembly.assemblyJobId).toBe(
      assembled.assembly.assemblyJobId,
    );
    expect(current.assembly.state).toBe("succeeded");
    expect(current.allowedActions).toContain(
      "import_production_outputs_to_authoring",
    );
    stage = "import";
    const beforeImportGraph = await readVisibleGraph(page);
    const beforeImportOwned = observeOwnedGraph(
      beforeImportGraph,
      produced.ownedReference,
    );
    // The editor's import is in the Clip editor's Media pane (the bin toolbar), not on
    // Production's own tab, which does not render it.
    await openOverlay("assets");
    await overlay
      .locator('[data-h3-nle-control="asset.import_production"]')
      .click();
    await expect.poll(() => imports.length, { timeout: 60_000 }).toBe(1);
    expect(imports[0]!.status, JSON.stringify(imports[0]!.body)).toBe(200);
    const imported = decodeProductionAuthoringImportResponse(imports[0]!.body);
    authoringHandle = imported.receipt.authoringWorkspaceHandle;
    expect(imported.receipt.productionWorkspaceId).toBe(current.workspaceId);
    expect(imported.receipt.rows).toHaveLength(3);
    expect(
      [...imports[0]!.request.entries].map((row: any) => row.segment_id).sort(),
    ).toEqual([...selectedIds].sort());
    // Import changes the library, never the timeline.
    expect(imported.receipt.nle.nextTimelineRevision).toBe(
      imported.receipt.nle.priorTimelineRevision,
    );
    await expect
      .poll(() =>
        authoringResponses.some(
          (row) => row.action === "read_timeline_history",
        ),
      )
      .toBe(true);
    const history = decodeTimelineHistoryProjection(
      authoringResponses.find((row) => row.action === "read_timeline_history")!
        .body,
    );
    expect(history.snapshot.clips).toHaveLength(0);
    expect(receipts).toHaveLength(0);
    const assetIds = imported.receipt.rows.map((row) => row.assetId);
    expect(
      history.snapshot.assets.filter((asset) =>
        assetIds.includes(asset.assetId),
      ),
    ).toHaveLength(3);
    observations.import = {
      receipt: imported.receipt,
      unimportedSegmentId: segmentIds.find((id) => !selectedIds.includes(id)),
    };
    mark("imported");

    // -------------------------------------------------------------- T1 separate insertion
    stage = "insertion";
    // The import left the overlay open; return to its Media pane if the import moved away.
    await expect(overlay).toHaveCount(1);
    const assetsPane = overlay.locator('[data-h3-nle-pane="assets"]');
    if ((await assetsPane.getAttribute("aria-selected")) !== "true")
      await assetsPane.click();
    // M25-63: the bin card shows the one-shot import cue as its "Imported" badge.
    for (const assetId of assetIds)
      await expect(
        overlay.locator(
          `[data-h3-nle-asset="${assetId}"] [data-h3-nle-media-badge="added"]`,
        ),
      ).toHaveText("Imported");
    const insert = overlay.locator(
      `[data-h3-nle-asset="${assetIds[0]}"] [data-h3-nle-control="asset.insert"]`,
    );
    const insertResponse = page.waitForResponse(
      (response) =>
        normalizeM2508HostApiPath(new URL(response.url()).pathname) ===
          AUTHORING_ROUTE &&
        response.request().postDataJSON()?.action ===
          "apply_timeline_transaction",
    );
    await insert.click();
    const inserted = await insertResponse;
    expect(inserted.status()).toBe(200);
    expect(
      inserted
        .request()
        .postDataJSON()
        .payload.commands.map((row: any) => row.kind),
    ).toEqual(["insert_asset_clip"]);
    await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);
    await expect.poll(() => receipts.length).toBe(1);
    const clipId = receipts[0]!.snapshot.clips[0]!.clipId;
    const timelineReady = () =>
      expect(
        overlay.locator('[data-h3-nle-status="timeline"]'),
      ).toHaveAttribute("data-h3-nle-authoring", "ready");
    mark("inserted");

    // -------------------------------------------------------------- T1 real edits
    stage = "edits";
    await timelineReady();
    await overlay
      .locator(
        `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
      )
      .click();
    await expect.poll(() => receipts.length).toBe(2);
    const clipOf = (receipt: TimelineReceipt) =>
      receipt.snapshot.clips.find((clip) => clip.clipId === clipId)!;
    const trims: Array<Record<string, unknown>> = [];
    // The "+" Add inserts the whole asset at the playhead, frame 0, so the first start trim
    // makes the room before the clip that `verify_causal_media.py` measures as silence.
    for (const [edge, deltaPixels] of [
      ["start", 72],
      ["start", -12],
      ["end", -24],
      ["end", 12],
    ] as const) {
      await timelineReady();
      const before = clipOf(receipts.at(-1)!);
      const count = receipts.length;
      // M25-62: a clip too narrow for inline grips carries them on the trim rail below it.
      const grip = overlay
        .locator(
          `[data-h3-nle-clip="${clipId}"] .h3-nle-grip[data-h3-nle-trim-edge="${edge}"]:not([hidden]), ` +
            `.h3-nle-trim-rail[data-h3-nle-trim-clip="${clipId}"] [data-h3-nle-trim-edge="${edge}"]`,
        )
        .first();
      await expect(grip).toBeEnabled();
      const box = await grip.boundingBox();
      if (!box) throw new Error("trim_grip_not_visible");
      const x = box.x + box.width / 2,
        y = box.y + box.height / 2;
      await page.mouse.move(x, y);
      await page.mouse.down();
      await page.mouse.move(x + deltaPixels, y, { steps: 4 });
      expect(receipts.length).toBe(count);
      await page.mouse.up();
      await expect.poll(() => receipts.length).toBe(count + 1);
      const after = clipOf(receipts.at(-1)!);
      const shortened = edge === "start" ? deltaPixels > 0 : deltaPixels < 0;
      if (shortened)
        expect(after.durationFrames).toBeLessThan(before.durationFrames);
      else expect(after.durationFrames).toBeGreaterThan(before.durationFrames);
      if (edge === "start") {
        expect(Math.sign(after.startFrame - before.startFrame)).toBe(
          Math.sign(deltaPixels),
        );
        expect(after.sourceStartFrame - before.sourceStartFrame).toBe(
          after.startFrame - before.startFrame,
        );
      } else expect(after.startFrame).toBe(before.startFrame);
      trims.push({ edge, deltaPixels, before, after });
    }
    // The media verifier hears 1.25 s before the clip and 1 s into it: at least 1.5 s of
    // timeline must precede the edited clip, and 3 s of it must remain.
    expect(clipOf(receipts.at(-1)!).startFrame).toBeGreaterThanOrEqual(36);
    expect(clipOf(receipts.at(-1)!).durationFrames).toBeGreaterThanOrEqual(72);
    await timelineReady();
    const beforeVisual = clipOf(receipts.at(-1)!);
    const inspector = overlay.locator(
      `[data-h3-nle-selected-clip="${clipId}"]`,
    );
    // The opacity group is on the inspector's Basic tab; 50 % is 5,000 bp.
    await inspector.locator('[data-h3-nle-property-tab="basic"]').click();
    await inspector
      .getByRole("spinbutton", { name: "Opacity (%)", exact: true })
      .fill("50");
    let count = receipts.length;
    await inspector
      .locator('[data-h3-nle-control="visual.opacity_blend"]')
      .click();
    await expect.poll(() => receipts.length).toBe(count + 1);
    const visual = clipOf(receipts.at(-1)!);
    expect(visual.opacityBp).toBe(5000);
    expect({ ...visual, opacityBp: beforeVisual.opacityBp }).toEqual(
      beforeVisual,
    );
    await timelineReady();
    count = receipts.length;
    await activate("history.undo");
    await expect.poll(() => receipts.length).toBe(count + 1);
    expect(clipOf(receipts.at(-1)!)).toEqual(beforeVisual);
    await timelineReady();
    await activate("history.redo");
    await expect.poll(() => receipts.length).toBe(count + 2);
    expect(clipOf(receipts.at(-1)!)).toEqual(visual);
    await timelineReady();
    observations.edits = { clipId, trims, beforeVisual, visual };
    mark("edited");

    // -------------------------------------------------------------- T1 transport and audio
    stage = "transport";
    const pixels = () =>
      // The monitor's picture only: the bin's thumbnails and the timeline are canvases too.
      overlay
        .locator(".h3-nle-monitor canvas")
        .evaluate((canvas: HTMLCanvasElement) => {
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
    const play = overlay.locator('[data-h3-nle-control="transport.play"]');
    await expect(play).toBeEnabled();
    // M25-62: the seek control is the timeline ruler, a `role="slider"` with no input value; its
    // frame is `aria-valuenow`, and Home/PageUp/ArrowRight keep their frozen steps.
    const seek = overlay.locator('[data-h3-nle-control="transport.seek"]');
    await seek.press("Home");
    await expect(seek).toHaveAttribute("aria-valuenow", "0");
    // Frame 0 precedes the inserted clip; the keyboard seek lands inside its edited range.
    const blank = await pixels();
    // The frozen M25-21 page step: PageUp is one output second forward, PageDown one back.
    for (let second = 0; second < 3; second++) await seek.press("PageUp");
    for (let frame = 0; frame < 12; frame++) await seek.press("ArrowRight");
    await expect(seek).toHaveAttribute("aria-valuenow", "84");
    const edited = clipOf(receipts.at(-1)!);
    expect(edited.startFrame).toBeLessThanOrEqual(84);
    expect(edited.startFrame + edited.durationFrames).toBeGreaterThan(84);
    await expect
      .poll(async () => (await pixels()).checksum)
      .not.toBe(blank.checksum);
    await expect.poll(async () => (await pixels()).colors).toBeGreaterThan(100);
    const browser = context.browser();
    if (!browser) throw new Error("test browser missing");
    const browserCdp = await browser.newBrowserCDPSession();
    const info = await browserCdp.send("SystemInfo.getProcessInfo");
    const browserPid = info.processInfo.find(
      (row) => row.type === "browser",
    )?.id;
    if (!browserPid) throw new Error("test browser PID missing");
    audioObserver = await startProcessAudioObserver(
      repositoryRoot,
      browserPid,
      ownedPath(fixture.audioObserver.observerRelative),
      fixture.audioObserver.observerSha256,
    );
    const playedAt = await page.evaluate(
      () => performance.timeOrigin + performance.now(),
    );
    await play.click();
    await expect(
      overlay.locator('[data-h3-nle-status="audio"]'),
    ).toHaveAttribute("data-h3-nle-audio-state", "following");
    await expect
      .poll(async () => Number(await seek.getAttribute("aria-valuenow")))
      .toBeGreaterThan(84);
    await page.waitForTimeout(1_500);
    await overlay.locator('[data-h3-nle-control="transport.pause"]').click();
    const pausedAt = await page.evaluate(
      () => performance.timeOrigin + performance.now(),
    );
    await expect(
      overlay.locator('[data-h3-nle-status="audio"]'),
    ).toHaveAttribute("data-h3-nle-audio-state", "suspended");
    // Pause must stop advancement, not only accept the click.
    let pausedFrame = -1;
    await expect
      .poll(async () => {
        const before = Number(await seek.getAttribute("aria-valuenow"));
        await page.waitForTimeout(250);
        pausedFrame = Number(await seek.getAttribute("aria-valuenow"));
        return pausedFrame === before;
      })
      .toBe(true);
    await page.waitForTimeout(1_000);
    await expect(seek).toHaveAttribute("aria-valuenow", String(pausedFrame));
    const audio = await audioObserver.stop();
    audioObserver = undefined;
    expect(audio.selector).toBe("include_test_browser_process_tree");
    expect(audio.rawAudioRetained || audio.microphoneOpened).toBe(false);
    expect(audio.browserExecutableSha256).toBe(
      fixture.audioObserver.browserExecutableSha256,
    );
    const audible = audio.packets.filter(
      (row) => row.time > playedAt && row.time < pausedAt && row.pcm16Peak > 0,
    );
    const audiblePeak = Math.max(0, ...audible.map((row) => row.pcm16Peak));
    // Recorded before judging, so a failing row still says what it heard.
    observations.transportAudio = {
      audiblePackets: audible.length,
      audiblePcm16Peak: audiblePeak,
      onsets: audio.onsets.length,
    };
    expect(audible.length).toBeGreaterThan(0);
    // IMPORTANT (B-M2522-SPEC-02): the observer's onset rule (amplitude > 0.1) belongs to the
    // M25-15 click fixtures. These producer inputs are lavfi sine tones at the default 1/8
    // amplitude, -20.8 dBFS (about 0.091) after AAC, so they can never register an onset. Judge
    // audibility with the tone floor `verify_causal_media.py` applies to the same inputs (0.01).
    expect(audiblePeak).toBeGreaterThan(0.01 * 32768);
    const stopped = audio.packets.filter((row) => row.time > pausedAt + 250);
    expect(stopped.length).toBeGreaterThan(0);
    expect(stopped.every((row) => row.pcm16Peak === 0)).toBe(true);
    observations.transport = {
      seekFrame: 84,
      pausedFrame,
      audiblePackets: audible.length,
      silentPacketsAfterPause: stopped.length,
      onsets: audio.onsets.length,
    };
    mark("transport");

    // -------------------------------------------------------------- T1 render, preview, download
    stage = "render";
    await timelineReady();
    // M25-44: the final-video card lives in the chrome bar's Export popover.
    await openExportPanel(overlay);
    const render = overlay.locator('[data-h3-nle-region="render"]');
    await expect(render).toHaveAttribute("data-h3-nle-render", "available");
    await render
      .getByRole("button", { name: "Render final video", exact: true })
      .click();
    await expect(render.getByRole("status").first()).toHaveText("Video ready", {
      timeout: 900_000,
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
    expect(outputStatus.currency).toBe("current");
    expect(outputStatus.timeline_revision).toBe(
      receipts.at(-1)!.snapshot.timelineRevision,
    );
    expect(outputStatus.output_handle).not.toBeNull();
    expect(counters.outputCreates).toBe(1);
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
    const previewBytes = await readReadyPreviewBytes(
      render.getByLabel("Final video preview", { exact: true }),
      output,
      OUTPUT_CAPABILITY.max_preview_bytes,
    );
    await writeFile(resolve(evidence, "nle-render-preview.mp4"), previewBytes);
    expect(counters.outputPreviews).toBe(1);
    const downloadEvent = page.waitForEvent("download", { timeout: 120_000 });
    await render
      .getByRole("link", { name: "Download original", exact: true })
      .click();
    const download = await downloadEvent;
    const downloadPath = resolve(evidence, "nle-render-download.mp4");
    await download.saveAs(downloadPath);
    expect(await download.failure()).toBeNull();
    const downloaded = await readFile(downloadPath);
    const downloadedSha256 = createHash("sha256")
      .update(downloaded)
      .digest("hex");
    expect(`sha256:${downloadedSha256}`).toBe(
      outputStatus.output!.output_fingerprint,
    );
    expect(downloaded.length).toBe(outputStatus.output!.byte_length);
    expect(counters.outputDownloads).toBe(1);
    observations.nle = {
      importReceipt: imported.receipt,
      insertion: await inserted.json(),
      outputStatus,
      renderedBytes: previewBytes.length,
      renderedSha256: createHash("sha256").update(previewBytes).digest("hex"),
      downloadedBytes: downloaded.length,
      downloadedSha256,
      suggestedFilename: download.suggestedFilename(),
    };
    mark("rendered");

    // -------------------------------------------------------------- T1 close and focus
    stage = "close";
    await closeOverlay();
    const afterImportGraph = await readVisibleGraph(page);
    expect(
      observeOwnedGraph(afterImportGraph, produced.ownedReference),
    ).toEqual(beforeImportOwned);
    observations.surroundings = diffGraphSurroundings({
      beforeValue: beforeImportGraph,
      afterValue: afterImportGraph,
      reference: {
        ownedNodeIds: produced.ownedReference.nodeIds,
        ownedLinkIds: produced.ownedReference.linkIds,
        anchorNodeId: produced.ownedReference.anchorNodeId,
        ownedProjectionEqual: true,
      },
    });
    expect(counters.promptPosts).toBe(5);
    expect(counters.imports).toBe(1);
    expect(
      counters.productionActions.filter(
        (action) => action === "assemble_sequence",
      ),
    ).toHaveLength(1);
    await hostQueueIdle();
    observations.t1 = {
      status: "pass",
      queue: { contextSetup: 1, managedChildren: 4, afterCompletion: 0 },
    };
    mark("t1_complete");
    withinCeiling(null, "t1_complete", 90);

    // -------------------------------------------------------------- T2 native Sidebar retention
    // T1's interval is closed above; T2 measures its own deltas and adds no generation effect.
    stage = "t2_native_mount";
    const t2Before = snapshotCounters();
    const t2Counts = {
      production: counters.productionActions.length,
      authoring: counters.authoringActions.length,
    };
    // Retire the reproduced T1 panel through the tab's own destroy; from here the host's native
    // sidebar is the only thing that mounts or unmounts the H3 view.
    await leaveH3View(page, CONTAINER_ID);
    const nativeTab = page.locator(
      '[data-testid="h3-context-tab-button"], .side-bar-button.h3-context-tab-button',
    );
    observations.t2SidebarCensus = await page
      .locator(".side-bar-button")
      .evaluateAll((nodes) =>
        nodes.slice(0, 24).map((node) => ({
          testId: node.getAttribute("data-testid")?.slice(0, 80) ?? null,
          selected: node.classList.contains("side-bar-button-selected"),
        })),
      );
    await expect(nativeTab).toHaveCount(1);
    // Another registered sidebar tab, never a bottom-bar dialog or panel toggle.
    const otherTabId = await page.evaluate(() => {
      const app = (window as unknown as { comfyAPI: { app: { app: any } } })
        .comfyAPI.app.app;
      return (
        app.extensionManager
          .getSidebarTabs()
          .map((tab: { id: string }) => tab.id)
          .find(
            (id: string) => id !== "h3-context" && /^[a-z0-9-]{1,60}$/.test(id),
          ) ?? null
      );
    });
    if (otherTabId === null) throw new Error("no other native sidebar tab");
    const otherTab = page.locator(
      `[data-testid="${otherTabId}-tab-button"], .side-bar-button.${otherTabId}-tab-button`,
    );
    const mounts = page.locator("[data-h3-context-mount]");
    // A host that persisted H3 as its open tab already holds a native mount; collapse it first.
    if ((await mounts.count()) > 0) {
      await nativeTab.click();
      await expect(mounts).toHaveCount(0);
    }
    await nativeTab.click();
    await expect(mounts).toHaveCount(1);
    shell = mounts.first();
    await expect(shell.locator("[data-shell-status]")).toHaveCount(1);
    const timelineZoom = overlay.locator(
      '[data-h3-nle-region="timeline"] .h3-nle-timeline-toolbar output',
    );
    const retainedView = async () => ({
      ...(await overlay.evaluate((node: HTMLElement) => ({
        width: node.style.width,
        height: node.style.height,
      }))),
      pane: await overlay
        .locator('[data-h3-nle-pane][aria-selected="true"]')
        .first()
        .getAttribute("data-h3-nle-pane"),
      frame: await overlay
        .locator('[data-h3-nle-control="transport.seek"]')
        .getAttribute("aria-valuenow"),
      snap: await overlay
        .locator('[data-h3-nle-control="transport.snap"]')
        .getAttribute("aria-pressed"),
      zoom: await timelineZoom.first().textContent(),
    });
    // Explicit Open without choosing a pane, so the pane itself is observed, never set.
    const openSurface = async () => {
      await shell.locator('[data-page-id="production"]').click();
      await shell.locator('[data-h3-director-function="clip_editor"]').click();
      await launcher().click();
      await expect(overlay).toHaveCount(1);
      await expect(overlay.locator("h2")).toBeFocused();
    };
    const openRetained = async () => {
      await openSurface();
      await expect(
        overlay.locator('[data-h3-nle-control="transport.seek"]'),
      ).toBeEnabled({ timeout: 60_000 });
      // A retained frame is restored once the monitor snapshot arrives; wait until it holds.
      await expect
        .poll(async () => {
          const seek = overlay.locator(
            '[data-h3-nle-control="transport.seek"]',
          );
          const before = await seek.getAttribute("aria-valuenow");
          await page.waitForTimeout(750);
          return before === (await seek.getAttribute("aria-valuenow"));
        })
        .toBe(true);
    };
    await openRetained();
    stage = "t2_adjust";
    const prior = await retainedView();
    const resize = overlay.locator('[data-h3-nle-action="resize"]');
    // Either direction is a real retained change; a bound already at its clamp moves the other way.
    const changeOnce = async (
      read: () => Promise<string | null>,
      first: () => Promise<void>,
      fallback: () => Promise<void>,
    ) => {
      const before = await read();
      await first();
      if ((await read()) === before) await fallback();
    };
    const overlaySize = (axis: "width" | "height") => () =>
      overlay.evaluate((node: HTMLElement, key) => node.style[key], axis);
    await changeOnce(
      overlaySize("width"),
      () => resize.press("ArrowLeft"),
      () => resize.press("ArrowRight"),
    );
    await changeOnce(
      overlaySize("height"),
      () => resize.press("ArrowUp"),
      () => resize.press("ArrowDown"),
    );
    const retainedSeek = overlay.locator(
      '[data-h3-nle-control="transport.seek"]',
    );
    const targetFrame = prior.frame === "100" ? "96" : "100";
    await retainedSeek.press("Home");
    for (let second = 0; second < 4; second++)
      await retainedSeek.press("PageUp");
    if (targetFrame === "100")
      for (let frame = 0; frame < 4; frame++)
        await retainedSeek.press("ArrowRight");
    await expect(retainedSeek).toHaveAttribute("aria-valuenow", targetFrame);
    // A late restore must never override the user's own seek.
    await page.waitForTimeout(1_500);
    await expect(retainedSeek).toHaveAttribute("aria-valuenow", targetFrame);
    await changeOnce(
      () => timelineZoom.first().textContent(),
      () =>
        overlay.locator('[data-h3-nle-control="transport.zoom_in"]').click(),
      () =>
        overlay.locator('[data-h3-nle-control="transport.zoom_out"]').click(),
    );
    await overlay.locator('[data-h3-nle-control="transport.snap"]').click();
    // M25-44: R1's two tabs are the retained pane; the inspector is permanently R3.
    const targetPane = prior.pane === "sequence" ? "assets" : "sequence";
    await overlay.locator(`[data-h3-nle-pane="${targetPane}"]`).first().click();
    const adjusted = await retainedView();
    // Every retained field must really change, or a restore would prove nothing.
    for (const field of [
      "width",
      "height",
      "pane",
      "frame",
      "snap",
      "zoom",
    ] as const)
      expect(adjusted[field], field).not.toBe(prior[field]);
    expect(adjusted.frame).toBe(targetFrame);
    expect(adjusted.pane).toBe(targetPane);
    await closeOverlay();
    const assertRestored = async (transition: string) => {
      await expect(mounts).toHaveCount(1);
      shell = mounts.first();
      await openRetained();
      await expect
        .poll(async () => (await retainedView()).frame, { message: transition })
        .toBe(targetFrame);
      expect(await retainedView(), transition).toEqual(adjusted);
      // Restored transport is paused: the frame holds and audio does not follow.
      await page.waitForTimeout(1_000);
      await expect(retainedSeek).toHaveAttribute("aria-valuenow", targetFrame);
      await expect(
        overlay.locator('[data-h3-nle-status="audio"]'),
      ).not.toHaveAttribute("data-h3-nle-audio-state", "following");
      await closeOverlay();
    };
    // With the view gone nothing owned may be requested: no command, read, poll or lease, across
    // more than one output-status and several sequence poll intervals.
    const quiet = async (transition: string) => {
      await Promise.all(responseTasks);
      const before = snapshotCounters();
      await page.waitForTimeout(7_000);
      expect(snapshotCounters(), transition).toEqual(before);
    };
    stage = "t2_collapse";
    await nativeTab.click();
    await expect(mounts).toHaveCount(0);
    await expect(overlay).toHaveCount(0);
    await quiet("native collapse");
    stage = "t2_expand";
    await nativeTab.click();
    await assertRestored("native expand");
    stage = "t2_tab_leave";
    await expect(otherTab).toHaveCount(1);
    observations.t2OtherTab = otherTabId;
    await otherTab.click();
    await expect(mounts).toHaveCount(0);
    await quiet("native tab leave");
    stage = "t2_tab_return";
    await nativeTab.click();
    await assertRestored("native tab return");
    await Promise.all(responseTasks);
    const t2After = snapshotCounters();
    expect({
      ...t2After,
      productionActions: 0,
      authoringActions: 0,
      ownedRequests: 0,
    }).toEqual({
      ...t2Before,
      productionActions: 0,
      authoringActions: 0,
      ownedRequests: 0,
    });
    const t2Production = counters.productionActions.slice(t2Counts.production);
    const t2Authoring = counters.authoringActions.slice(t2Counts.authoring);
    expect(
      t2Production.filter((action) => action !== "read_projection"),
    ).toEqual([]);
    expect(t2Authoring.filter((action) => !action.startsWith("read_"))).toEqual(
      [],
    );
    await hostQueueIdle();
    observations.t2 = {
      status: "pass",
      locale: "en",
      viewport: page.viewportSize(),
      prior,
      retained: adjusted,
      transitions: [
        "native collapse",
        "native expand",
        "native tab leave",
        "native tab return",
      ],
      productionActions: t2Production,
      authoringActions: t2Authoring,
      counters: { before: t2Before, after: t2After },
    };
    mark("t2_complete");
    withinCeiling("t1_complete", "t2_complete", 20);

    // -------------------------------------------------------------- T3 revoked-source boundary
    // One same-owner release of the Production workspace whose originals were imported. The
    // imported clip's source is revoked: no later preview or render may use it, and T1's
    // already-rendered output is not a stale output.
    stage = "t3_release";
    await Promise.all(responseTasks);
    const t3Before = snapshotCounters();
    const t3Leases = leaseResponses.length;
    const t3Creates = outputCreateResponses.length;
    const t1Job = (
      observations.nle as {
        outputStatus: ReturnType<typeof decodeOutputStatus>;
      }
    ).outputStatus;
    const owner = await readProduction(productionHandle!);
    const released = await post(
      page,
      PRODUCTION_ROUTE,
      encodeProductionAction(requestId("t3.release"), "release_workspace", {
        projection: owner,
      }),
    );
    expect(released.status).toBe(204);
    productionReleasedByT3 = true;
    stage = "t3_preview";
    // IMPORTANT (B-M2522-SPEC-04): the restore asks for the retained frame's source inside the
    // edited clip, and the revoked source then blocks the monitor at any moment, disabling the
    // transport and showing "Frame unavailable" rather than that frame. Open the surface without
    // requiring an enabled or stable transport: the typed refusal of that ask and the undrawn
    // canvas are the T3 evidence below, not the slider.
    await openSurface();
    // Let the monitor attempt its source for the retained frame inside the edited clip.
    await page.waitForTimeout(5_000);
    await Promise.all(responseTasks);
    const t3LeaseRows = leaseResponses.slice(t3Leases);
    const t3SourceAsks = t3LeaseRows.filter(
      (row) => row.operation !== "release",
    );
    // The monitor really asked for the source, and every ask is refused with a typed revocation:
    // a new lease as stale (never a generation failure the browser would retry), an earlier
    // lease as gone. No ask after the release obtains a lease or its bytes.
    expect(t3SourceAsks.length, JSON.stringify(t3LeaseRows)).toBeGreaterThan(0);
    expect(
      t3SourceAsks.filter(
        (row) =>
          !(
            row.operation === "create" &&
            row.status === 409 &&
            row.reason === "stale"
          ) &&
          !(
            row.operation !== "create" &&
            row.status === 410 &&
            row.reason === "lease_gone"
          ) &&
          !(
            row.operation !== "create" &&
            row.status === 409 &&
            row.reason === "stale"
          ),
      ),
      JSON.stringify(t3LeaseRows),
    ).toEqual([]);
    // T1 proved a drawn source frame exceeds 100 lit samples; the revoked source draws none.
    const t3Pixels = [];
    for (let sample = 0; sample < 3; sample++) {
      t3Pixels.push(await pixels());
      await page.waitForTimeout(500);
    }
    expect(
      t3Pixels.every((row) => row.colors <= 100),
      JSON.stringify(t3Pixels),
    ).toBe(true);
    const t3Monitor = await overlay
      .locator('[data-h3-nle-status="monitor"]')
      .textContent();
    stage = "t3_render";
    await openExportPanel(overlay);
    const t3Render = overlay.locator('[data-h3-nle-region="render"]');
    const t3RenderButton = t3Render.getByRole("button", {
      name: "Render final video",
      exact: true,
    });
    const renderAttempted = await t3RenderButton.isEnabled();
    if (renderAttempted) {
      await t3RenderButton.click();
      await expect
        .poll(() => outputCreateResponses.length, { timeout: 60_000 })
        .toBe(t3Creates + 1);
      // A typed lost-authority refusal: nothing is planned, queued or retained.
      expect(outputCreateResponses.at(-1)).toEqual({
        status: 404,
        code: "unavailable",
        phase: null,
        jobHandle: null,
      });
    }
    // Hold beyond the output poll interval: no new job may appear or succeed.
    await page.waitForTimeout(12_000);
    await Promise.all(responseTasks);
    const t3Outputs = outputStatuses.filter(
      (row) =>
        row.workspace_handle === authoringHandle &&
        row.job_handle !== t1Job.job_handle,
    );
    expect(t3Outputs).toEqual([]);
    expect(counters.outputCreates).toBe(
      t3Before.outputCreates + (renderAttempted ? 1 : 0),
    );
    await closeOverlay();
    await Promise.all(responseTasks);
    const t3After = snapshotCounters();
    expect(t3After.promptPosts).toBe(t3Before.promptPosts);
    expect(t3After.imports).toBe(t3Before.imports);
    expect(t3After.managedActions).toBe(t3Before.managedActions);
    await hostQueueIdle();
    observations.t3 = {
      status: "pass",
      release: { action: "release_workspace", status: released.status },
      leaseResponses: t3LeaseRows,
      monitorStatus: t3Monitor?.slice(0, 120) ?? null,
      pixels: t3Pixels,
      renderAttempted,
      renderCreates: outputCreateResponses.slice(t3Creates),
      newOutputStatuses: t3Outputs.length,
      counters: { before: t3Before, after: t3After },
    };
    mark("t3_complete");
    withinCeiling("t2_complete", "t3_complete", 20);
    stage = "complete";
  } catch (error) {
    primaryFailure = error;
    observations.failedStage = stage;
    observations.monitorAtFailure = await page
      .evaluate(() => ({
        // Repository-owned localized status only, never user text.
        monitor:
          document
            .querySelector('[data-h3-nle-status="monitor"]')
            ?.textContent?.slice(0, 240) ?? null,
        audio:
          document
            .querySelector('[data-h3-nle-status="audio"]')
            ?.getAttribute("data-h3-nle-audio-state") ?? null,
        sequence:
          document
            .querySelector('[data-h3-nle-status="sequence"]')
            ?.getAttribute("data-state") ?? null,
        sequenceFailure:
          document
            .querySelector('[data-h3-nle-status="sequence-failure"]')
            ?.textContent?.slice(0, 160) ?? null,
        planning:
          document
            .querySelector("[data-h3-nle-planning-status]")
            ?.getAttribute("data-h3-nle-planning-status") ?? null,
        planningError:
          document
            .querySelector('[data-h3-nle-status="planning-error"]')
            ?.textContent?.slice(0, 160) ?? null,
        readiness:
          document
            .querySelector('[data-h3-nle-status="readiness"]')
            ?.getAttribute("data-state") ?? null,
        // M25-63: a held readiness names its closed reason code; without it a hold cannot be
        // localized after the run.
        readinessReason:
          document
            .querySelector('[data-h3-nle-status="readiness-reason"]')
            ?.getAttribute("data-code") ?? null,
      }))
      .catch(() => null);
  } finally {
    const cleanupErrors: unknown[] = [];
    const cleanupStage = async (
      name: string,
      operation: () => Promise<unknown>,
    ) => {
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
    await cleanupStage("audioObserver", async () => {
      if (!audioObserver) return "not_running";
      await audioObserver.stop();
      return "stopped";
    });
    await cleanupStage("closeOverlay", async () => {
      const close = overlay.locator('[data-h3-nle-action="close"]');
      if (await close.isVisible()) await close.click();
      await Promise.all(responseTasks);
      return "closed_or_absent";
    });
    await cleanupStage("producer", async () => {
      if (!producer) return "not_installed";
      await producer.dispose();
      return "disposed";
    });
    await cleanupStage("managedParent", async () => {
      if (!producerStarted) return "not_started";
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
        { bundleUrl, priorParentId },
      );
      if (snapshot.parentSequenceId)
        expect(["succeeded", "cancelled"]).toContain(snapshot.parentState);
      await expect
        .poll(() => supportedHostQueueCounts(page), { timeout: 120_000 })
        .toEqual({ running: 0, pending: 0 });
      return snapshot;
    });
    await cleanupStage("renderJob", async () => {
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
    await cleanupStage("assemblyJob", async () => {
      if (!productionHandle) return "not_created";
      if (productionReleasedByT3) return "released_by_t3";
      const fresh = await readProduction(productionHandle);
      if (fresh.allowedActions.includes("cancel_assembly")) {
        const cancelled = await post(
          page,
          PRODUCTION_ROUTE,
          encodeProductionAction(
            requestId("assembly.cancel"),
            "cancel_assembly",
            { projection: fresh },
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
    await cleanupStage("authoringRelease", async () => {
      if (!authoringHandle) return "not_created";
      const result = await post(
        page,
        AUTHORING_ROUTE,
        encodeAuthoringAction(
          requestId("authoring.release"),
          "release_workspace",
          { workspace_handle: authoringHandle },
        ),
      );
      expect(result.status).toBe(204);
      return result.status;
    });
    await cleanupStage("productionRelease", async () => {
      if (!productionHandle) return "not_created";
      if (productionReleasedByT3) {
        // T3 already released it; the owner must now be gone rather than silently live.
        const gone = await post(
          page,
          PRODUCTION_ROUTE,
          encodeProductionAction(
            requestId("read.released"),
            "read_projection",
            {
              workspaceHandle: productionHandle,
            },
          ),
        );
        expect([404, 410]).toContain(gone.status);
        return "released_by_t3";
      }
      const fresh = await readProduction(productionHandle);
      const result = await post(
        page,
        PRODUCTION_ROUTE,
        encodeProductionAction(requestId("release"), "release_workspace", {
          projection: fresh,
        }),
      );
      expect(result.status).toBe(204);
      return result.status;
    });
    await cleanupStage("observerStop", async () => {
      const stopped = await ownerCapture("stop");
      expect(stopped.capture.observerRestored).toBe(true);
      if (!primaryFailure && observationStarted)
        expect(stopped.capture.workerSubmissions).toBe(1);
      return stopped.capture;
    });
    page.off("request", onRequest);
    page.off("response", onResponse);
    page.off("requestfailed", onRequestFailed);
    page.off("pageerror", onPageError);
    page.off("download", onDownload);
    await Promise.all(responseTasks);
    cleanupErrors.push(...responseErrors);
    observations.cleanup = cleanup;
    observations.counters = counters;
    observations.leaseResponses = leaseResponses.slice(-64);
    observations.ownedFailures = ownedFailures;
    observations.pageErrors = pageErrors;
    observations.monitorTimeline = monitorTimeline;
    observations.productionResponses = productionResponses.map((row) => ({
      action: row.action,
      status: row.status,
    }));
    observations.runtimeResponses = runtimeResponses;
    observations.importResponses = imports.map((row) => ({
      status: row.status,
      entries: row.request?.entries ?? null,
    }));
    observations.authoringResponses = authoringResponses.map((row) => ({
      action: row.action,
      status: row.status,
    }));
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
    await cleanupStage("evidenceWrite", async () => {
      await writeFile(
        resolve(evidence, "runtime-observations.json"),
        JSON.stringify(observations, null, 2),
      );
    });
    await cleanupStage("evidenceAttachment", async () => {
      await testInfo.attach("nle-causal-journey", {
        body: JSON.stringify({ outcome: observations.outcome, stage }),
        contentType: "application/json",
      });
    });
    if (primaryFailure || cleanupErrors.length)
      throw new AggregateError(
        [...(primaryFailure ? [primaryFailure] : []), ...cleanupErrors],
        "causal journey or owned cleanup failed",
      );
  }
  expect(cleanup.authoringRelease).toEqual({ status: "pass", result: 204 });
  expect(cleanup.productionRelease).toEqual({
    status: "pass",
    result: productionReleasedByT3 ? "released_by_t3" : 204,
  });
});
