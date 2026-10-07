import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { relative, resolve } from "node:path";

import type { Locator, Page } from "@playwright/test";

// CRITICAL: Node-hosted Playwright rejects JSON imports without this attribute.
import fontManifest from "../../../../../comfyui_h3_context/fonts/font_manifest_v1.json" with { type: "json" };

import {
  decodeAuthoringProjection,
  decodeTimelineHistoryProjectionV2,
  decodeTimelineReceiptV2,
  encodeAuthoringAction,
  encodeTimelineTransactionV2,
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import {
  assertCandidateBundleInjection,
  candidateBackendMode,
  candidateBackendRuntime,
  candidateBundle,
  candidateInjectionCount,
  hostUrl,
  repositoryRoot,
} from "../../host/candidate";
import { test, expect } from "../../host/fixture";
import { supportedHostQueueCounts } from "../../host/managed";
import {
  beginSettledH3InteractionPhase,
  captureM17CanonicalIdentity,
  expectCandidateInteractionNetworkLocal,
  monitorCandidateInitiatorNetwork,
  monitorH3Network,
  waitForH3Registration,
} from "../../host/network";
import {
  describeM25LeaseUrl,
  matchesM25LeaseRoute,
  observeM25LeaseOperation,
  m25WorkspaceResourcesReleased,
  type M25PackagedFontBody,
  type M25LeaseOperationObservation,
  type M25LeaseUrlObservation,
} from "../../support/host/m25_52LeaseObservation";

const FIXTURE_ENV = "H3_CONTEXT_NLE_WORKSPACE_FIXTURE";
const RESOURCE_ENV = "H3_CONTEXT_M25_52_RESOURCE_RECEIPT";
const SHORT_ROUTE_ENV = "H3_CONTEXT_M25_52_SHORT_ROUTE";
const INSTALLED_INVENTORY_ENV = "H3_CONTEXT_CANDIDATE_INVENTORY_SHA256";

type HostResponse = Readonly<{ status: number; body: unknown }>;
type WorkspaceFixture = Readonly<{
  schema: "NleWorkspaceHostFixtureV1";
  output: unknown;
  anchorId: string;
  promptId: string;
  qualification: Readonly<{
    schema: "M25RealRuntimeQualificationV1";
    candidateBundleSha256: string;
  }>;
}>;
type ResourceCounts = Readonly<{
  leases: number;
  cacheEntries: number;
  cacheBytes: number;
  activeReads: number;
  decorationLeases: number;
  videoLeases: number;
}>;
type ResourceReceipt = Readonly<{
  schema: "M25_52ResourceObservationV2";
  candidateBundleSha256: string;
  sampleCount: number;
  maximum: ResourceCounts;
  current: ResourceCounts;
  retainedPackagedFonts: readonly M25PackagedFontBody[];
}>;

const candidateFontBodies = fontManifest.font_assets.flatMap(({ faces }) =>
  faces.map((face) => ({
    sha256: face.file_sha256,
    byteCount: face.size_bytes,
  })),
);

function record(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${name} is malformed`);
  return value as Record<string, unknown>;
}

function privateInputPath(environmentName: string): string {
  const configured = process.env[environmentName]?.trim() ?? "";
  if (configured.length === 0)
    throw new Error(`${environmentName} is required`);
  const absolute = resolve(repositoryRoot, configured);
  const inside = relative(repositoryRoot, absolute);
  if (
    inside === "" ||
    inside.startsWith("..") ||
    resolve(repositoryRoot, inside) !== absolute ||
    !inside.replaceAll("\\", "/").startsWith(".planning/evidence/")
  )
    throw new Error(`${environmentName} must name private repository evidence`);
  return absolute;
}

async function openTrackMenu(page: Page, trackId: string) {
  await page
    .locator(
      `[data-h3-nle-track="${trackId}"] [data-h3-nle-menu-trigger="track"]`,
    )
    .click();
  const menu = page.getByRole("menu", { name: "Track menu" });
  await expect(menu).toBeVisible();
  return menu;
}

async function seekPlayheadByKeyboard(
  page: Page,
  slider: Locator,
  frame: number,
) {
  await expect(slider).toHaveAttribute("aria-disabled", "false");
  await slider.focus();
  await page.keyboard.press("Home");
  await expect(slider).toHaveAttribute(
    "data-h3-nle-request-clear-reason",
    "monitor_request_settled",
  );
  for (let current = 0; current < frame; current += 1) {
    await page.keyboard.press("ArrowRight");
    await expect(
      slider,
      `ArrowRight ${current + 1} did not settle at target frame ${current + 1}`,
    ).toHaveAttribute(
      "data-h3-nle-request-clear-reason",
      "monitor_request_settled",
    );
  }
  await expect(slider).toHaveAttribute("aria-valuenow", String(frame));
}

async function loadWorkspaceFixture(): Promise<WorkspaceFixture> {
  const wire = record(
    JSON.parse(await readFile(privateInputPath(FIXTURE_ENV), "utf8")),
    "workspace fixture",
  );
  const qualification = record(wire.qualification, "workspace qualification");
  if (
    wire.schema !== "NleWorkspaceHostFixtureV1" ||
    qualification.schema !== "M25RealRuntimeQualificationV1" ||
    qualification.candidateBundleSha256 !== candidateBundle?.sha256 ||
    typeof wire.anchorId !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(wire.anchorId) ||
    typeof wire.promptId !== "string" ||
    wire.promptId.length === 0
  )
    throw new Error("workspace fixture is not bound to the exact candidate");
  return wire as WorkspaceFixture;
}

function counts(value: unknown, name: string): ResourceCounts {
  const wire = record(value, name);
  const result = {
    leases: wire.leases,
    cacheEntries: wire.cacheEntries,
    cacheBytes: wire.cacheBytes,
    activeReads: wire.activeReads,
    decorationLeases: wire.decorationLeases,
    videoLeases: wire.videoLeases,
  };
  if (
    Object.values(result).some(
      (member) => !Number.isSafeInteger(member) || Number(member) < 0,
    )
  )
    throw new Error(`${name} has invalid resource counts`);
  return result as ResourceCounts;
}

async function loadResourceReceipt(): Promise<ResourceReceipt> {
  const wire = record(
    JSON.parse(await readFile(privateInputPath(RESOURCE_ENV), "utf8")),
    "resource receipt",
  );
  if (
    wire.schema !== "M25_52ResourceObservationV2" ||
    wire.candidateBundleSha256 !== candidateBundle?.sha256 ||
    !Number.isSafeInteger(wire.sampleCount) ||
    Number(wire.sampleCount) < 1 ||
    !Array.isArray(wire.retainedPackagedFonts) ||
    wire.retainedPackagedFonts.length > candidateFontBodies.length
  )
    throw new Error("resource receipt is not bound to the exact candidate");
  return {
    schema: "M25_52ResourceObservationV2",
    candidateBundleSha256: String(wire.candidateBundleSha256),
    sampleCount: Number(wire.sampleCount),
    maximum: counts(wire.maximum, "maximum resources"),
    current: counts(wire.current, "current resources"),
    retainedPackagedFonts: (wire.retainedPackagedFonts as unknown[]).map(
      (value) => {
        const body = record(value, "retained packaged font");
        if (
          Object.keys(body).sort().join(",") !== "byteCount,sha256" ||
          typeof body.sha256 !== "string" ||
          !/^sha256:[0-9a-f]{64}$/.test(body.sha256) ||
          !Number.isSafeInteger(body.byteCount) ||
          Number(body.byteCount) <= 0
        )
          throw new Error("retained packaged font proof is malformed");
        return { sha256: body.sha256, byteCount: Number(body.byteCount) };
      },
    ),
  };
}

async function postAction(page: Page, body: unknown): Promise<HostResponse> {
  return page.evaluate(async (requestBody) => {
    const response = await fetch("/h3-context/v1/authoring/action", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(requestBody),
    });
    const text = await response.text();
    return {
      status: response.status,
      body: text.length === 0 ? null : (JSON.parse(text) as unknown),
    };
  }, body);
}

const collectCompleteFrameSamples = (
  canvas: Locator,
  endFrameExclusive: number,
): Promise<number[]> =>
  canvas.evaluate(async (element, endExclusive) => {
    const samples: number[] = [];
    let last = -1;
    const append = (raw: string | null) => {
      const frame = raw === null ? -1 : Number(raw);
      if (Number.isSafeInteger(frame) && frame >= 0 && frame !== last) {
        samples.push(frame);
        last = frame;
      }
    };
    append(element.getAttribute("data-h3-nle-presented-frame"));
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) append(mutation.oldValue);
      append(element.getAttribute("data-h3-nle-presented-frame"));
    });
    observer.observe(element, {
      attributes: true,
      attributeFilter: ["data-h3-nle-presented-frame"],
      attributeOldValue: true,
    });
    // IMPORTANT: stop through the real pause control at the end of the measured transition.
    // Sampling a longer traversal and accepting gaps would hide dropped presentation frames.
    const deadline = performance.now() + 30_000;
    while (performance.now() < deadline) {
      append(element.getAttribute("data-h3-nle-presented-frame"));
      const pause = document.querySelector<HTMLButtonElement>(
        '[data-h3-nle-control="transport.pause"]',
      );
      if (last >= endExclusive - 1 && pause !== null) {
        pause.click();
        break;
      }
      // IMPORTANT: React briefly removes Play before it commits Pause. That handoff is not a
      // completed transport interval; ending here truncates a valid 12..23 frame observation.
      await new Promise<void>((resolveFrame) =>
        requestAnimationFrame(() => resolveFrame()),
      );
    }
    for (const mutation of observer.takeRecords()) append(mutation.oldValue);
    append(element.getAttribute("data-h3-nle-presented-frame"));
    observer.disconnect();
    return samples;
  }, endFrameExclusive);

test("M25-52 proves the real peaks route and mixed-load playback on the supplied host", async ({
  page,
  context,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_M25_52_HOST !== "1",
    "explicit M25-52 supplied-host row required",
  );
  test.setTimeout(300_000);
  await page.setViewportSize({ width: 1402, height: 868 });
  expect(page.viewportSize()).toEqual({ width: 1402, height: 868 });
  const installedCandidateInventorySha256 =
    process.env[INSTALLED_INVENTORY_ENV];
  if (
    !hostUrl ||
    !process.env.H3_CONTEXT_HOST_ROOT ||
    candidateBundle === null ||
    candidateBackendMode !== "exact" ||
    candidateBackendRuntime === null ||
    installedCandidateInventorySha256 === undefined ||
    !/^[0-9a-f]{64}$/.test(installedCandidateInventorySha256)
  )
    throw new Error(
      "M25-52 requires an explicitly supplied exact frontend/backend candidate",
    );

  const workspaceFixture = await loadWorkspaceFixture();
  const allowedOrigin = new URL(hostUrl).origin;
  const network = monitorH3Network(page, allowedOrigin);
  const candidateNetwork = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const operations: Record<string, Record<string, number>> = {};
  const activePlayback = new Set<string>();
  const activeDecorations = new Set<string>();
  const playbackHistory: Array<
    Readonly<{ operation: string; ownerId: string; at: number }>
  > = [];
  let maximumPlaybackOwners = 0;
  let maximumBrowserTrackedDecorations = 0;
  let pendingDecorationOpens = 0;
  let maximumBrowserAcquisitions = 0;
  let authoringHandle: string | undefined;
  let authoringProjection:
    ReturnType<typeof decodeAuthoringProjection> | undefined;
  let promptCalls = 0;
  let authoringCalls = 0;
  let playbackStartedAt = 0;
  let playbackFinishedAt = 0;
  const decorationCompletions: Array<Readonly<{ kind: string; at: number }>> =
    [];
  type LeaseRequestObservation = {
    url: string;
    urlFacts: M25LeaseUrlObservation;
    operation: string;
    requestId: string;
    ownerId: string;
    kind: string;
    at: number;
    endedAt: number | null;
    outcome: string | null;
  };
  let mediaBinOverlapArmed = false;
  let mediaBinTriggerRequest: LeaseRequestObservation | undefined;
  const overlapCandidates: LeaseRequestObservation[] = [];
  let overlapPlaybackTriggeredAt: number | null = null;
  let overlapPendingAtPlaybackTrigger = false;
  let overlapVideoOwners: string[] = [];
  let mediaBinOpenedAtFrame: number | null = null;
  let mediaBinDemandUnfinishedAtTransition = false;
  let mediaBinHttpPendingAtTransition = false;
  const leaseRequests: LeaseRequestObservation[] = [];
  const decorationRequests: LeaseRequestObservation[] = [];
  const leaseResponses: Array<
    Readonly<{
      url: string;
      urlFacts: M25LeaseUrlObservation;
      requestId: string;
      ownerId: string;
      kind: string;
      operation: string;
      status: number;
      disposition?: string;
    }>
  > = [];
  const timelineReceipts: ReturnType<typeof decodeTimelineReceiptV2>[] = [];
  const timelineRejections: unknown[] = [];
  const ownerKinds = new Map<string, string>();
  const pendingDecorationCreates = new Set<object>();
  const leaseRequestByIdentity = new WeakMap<object, LeaseRequestObservation>();

  const requestObservations = new WeakMap<
    object,
    M25LeaseOperationObservation
  >();
  const observeRequest = (request: any) => {
    if (request.method() !== "POST") return;
    const path = new URL(request.url()).pathname.replace(
      /^\/api(?=\/h3-context\/)/,
      "",
    );
    if (/\/(?:api\/)?prompt$/.test(path)) promptCalls += 1;
    if (path === "/h3-context/v1/authoring/action") authoringCalls += 1;
    const body = request.postDataJSON?.() ?? {};
    const leaseObservation = observeM25LeaseOperation(
      request.url(),
      body,
      ownerKinds,
    );
    if (leaseObservation === null) return;
    requestObservations.set(request, leaseObservation);
    const { operation, kind } = leaseObservation;
    const requestObservation = {
      url: request.url(),
      urlFacts: describeM25LeaseUrl(request.url()),
      operation,
      requestId: String(body.requestId ?? ""),
      ownerId: leaseObservation.ownerId,
      kind,
      at: performance.now(),
      endedAt: null,
      outcome: null,
    };
    leaseRequests.push(requestObservation);
    leaseRequestByIdentity.set(request, requestObservation);
    operations[kind] ??= {};
    operations[kind]![operation] = (operations[kind]![operation] ?? 0) + 1;
    if (
      operation === "create" &&
      ["thumbnail", "filmstrip", "audio_peaks"].includes(kind)
    ) {
      decorationRequests.push(requestObservation);
      pendingDecorationCreates.add(request);
    }
    if (
      mediaBinOverlapArmed &&
      operation === "create" &&
      kind === "thumbnail"
    ) {
      // IMPORTANT: retain every real Media-bin create. Playback intentionally aborts and settles
      // active decoration HTTP before acquiring its owners, so later evidence must distinguish a
      // queued UI demand from a request that is still executing in the backend.
      overlapCandidates.push(requestObservation);
    }
    if (
      operation === "open" &&
      ["thumbnail", "filmstrip", "audio_peaks"].includes(kind)
    ) {
      pendingDecorationOpens += 1;
      maximumBrowserAcquisitions = Math.max(
        maximumBrowserAcquisitions,
        pendingDecorationOpens,
      );
    }
  };
  const observeResponse = async (response: any) => {
    const request = response.request();
    const path = new URL(response.url()).pathname.replace(
      /^\/api(?=\/h3-context\/)/,
      "",
    );
    if (
      path === "/h3-context/v1/authoring/action" &&
      response.status() === 201 &&
      request.postDataJSON()?.action === "create_authoring_workspace"
    ) {
      authoringProjection = decodeAuthoringProjection(await response.json());
      authoringHandle = authoringProjection.workspaceHandle;
      return;
    }
    if (
      path === "/h3-context/v1/authoring/action" &&
      request.postDataJSON()?.action === "apply_timeline_transaction"
    ) {
      const body = await response.json();
      if (response.status() === 200)
        timelineReceipts.push(decodeTimelineReceiptV2(body));
      else timelineRejections.push({ status: response.status(), body });
      return;
    }
    if (!matchesM25LeaseRoute(response.url())) return;
    const observation = requestObservations.get(request);
    if (observation === undefined)
      throw new Error("lease response has no request observation");
    const { operation, ownerId, kind } = observation;
    const status = response.status();
    let disposition: string | undefined;
    if (status >= 400) {
      try {
        const failure = record(await response.json(), "lease failure response");
        if (typeof failure.disposition === "string")
          disposition = failure.disposition;
      } catch {
        disposition = "unreadable_failure";
      }
    }
    leaseResponses.push({
      url: response.url(),
      urlFacts: describeM25LeaseUrl(response.url()),
      requestId: String(request.postDataJSON?.()?.requestId ?? ""),
      ownerId,
      kind,
      operation,
      status,
      ...(disposition ? { disposition } : {}),
    });
    const decoration = ["thumbnail", "filmstrip", "audio_peaks"].includes(kind);
    if (operation === "create" && decoration)
      pendingDecorationCreates.delete(request);
    const requestObservation = leaseRequestByIdentity.get(request);
    if (requestObservation !== undefined) {
      requestObservation.endedAt = performance.now();
      requestObservation.outcome = `response:${status}`;
    }
    if (operation === "open" && decoration) {
      pendingDecorationOpens = Math.max(0, pendingDecorationOpens - 1);
      if (response.ok())
        decorationCompletions.push({ kind, at: performance.now() });
    }
    if (!response.ok() && response.status() !== 204) return;
    if (operation === "create") {
      if (kind === "video_proxy") {
        activePlayback.add(ownerId);
        playbackHistory.push({ operation, ownerId, at: performance.now() });
        maximumPlaybackOwners = Math.max(
          maximumPlaybackOwners,
          activePlayback.size,
        );
      } else if (decoration) {
        activeDecorations.add(ownerId);
        maximumBrowserTrackedDecorations = Math.max(
          maximumBrowserTrackedDecorations,
          activeDecorations.size,
        );
      }
    } else if (operation === "release") {
      // IMPORTANT: creating an audio preview rebinds the owner's remembered derivative kind, and
      // its release clears that memory before the owner-level control release arrives. Treat that
      // final control release as authoritative for any remaining video/decorative lease or the
      // observer reports a leak after the backend resource receipt has already reached zero.
      if (
        (kind === "video_proxy" || kind === "control") &&
        activePlayback.delete(ownerId)
      ) {
        playbackHistory.push({ operation, ownerId, at: performance.now() });
      }
      if (decoration || kind === "control") activeDecorations.delete(ownerId);
    }
  };
  const observeRequestFailed = (request: object) => {
    pendingDecorationCreates.delete(request);
    const lease = requestObservations.get(request);
    if (
      lease?.operation === "open" &&
      ["thumbnail", "filmstrip", "audio_peaks"].includes(lease.kind)
    )
      pendingDecorationOpens = Math.max(0, pendingDecorationOpens - 1);
    const observation = leaseRequestByIdentity.get(request);
    if (observation !== undefined && observation.endedAt === null) {
      observation.endedAt = performance.now();
      observation.outcome = "requestfailed";
    }
  };
  page.on("request", observeRequest);
  page.on("response", observeResponse);
  page.on("requestfailed", observeRequestFailed);

  const injectionBefore = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injectionBefore);
  await beginSettledH3InteractionPhase(page, network, candidateNetwork);
  const graphBefore = await captureM17CanonicalIdentity(page);
  const queueBefore = await supportedHostQueueCounts(page);
  const pageCount = context.pages().length;

  await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    runtime.__h3M2552HostBaseline = {
      activeWorkflow: app.extensionManager.workflow.activeWorkflow,
      openWorkflowCount: app.extensionManager.workflow.openWorkflows.length,
      ownedMountCount: document.querySelectorAll("[data-h3-context-mount]")
        .length,
    };
  });
  await page.evaluate(({ output, anchorId, promptId }) => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((entry: any) => entry.id === "h3-context");
    const panel = document.createElement("div");
    panel.id = "h3-m25-52-host-container";
    panel.className = "side-bar-panel sidebar-content-container";
    Object.assign(panel.style, {
      position: "fixed",
      inset: "48px auto 0 24px",
      width: "720px",
      height: "820px",
      overflow: "auto",
      zIndex: "2000",
      background: "#202124",
    });
    document.body.append(panel);
    tab.render(panel);
    const anchor = runtime.LiteGraph.createNode(
      "comfyui_h3_context.H3Context.ProductShell",
    );
    const native = runtime.LiteGraph.createNode("MiniMaxH3ReferenceToVideo");
    if (!anchor || app.graph.getNodeById(anchorId))
      throw new Error("owned anchor unavailable");
    if (!native) throw new Error("supplied native H3 registration unavailable");
    anchor.id = anchorId;
    app.graph.add(anchor);
    app.graph.add(native);
    runtime.__h3M2552Anchor = anchor;
    runtime.__h3M2552Native = native;
    runtime.comfyAPI.api.api.dispatchEvent(
      new CustomEvent("executed", {
        detail: { node: anchorId, prompt_id: promptId, output },
      }),
    );
  }, workspaceFixture);

  const shell = page.locator("#h3-m25-52-host-container");
  // IMPORTANT: registration can finish while ComfyUI's first-load splash still covers the sidebar.
  await expect(page.locator("#splash-loader")).toBeHidden();
  await shell.locator('[data-page-id="production"]').click();
  await shell.locator('[data-h3-director-function="clip_editor"]').click();
  const start = shell.getByRole("button", {
    name: "Start authoring from this context",
    exact: true,
  });
  await start.click();
  await expect(start).toHaveCount(0);
  await expect.poll(() => authoringHandle).not.toBeUndefined();
  await expect.poll(() => authoringProjection).not.toBeUndefined();
  const suffix = authoringHandle!.slice(-16).replace(/[^A-Za-z0-9]/g, "-");

  const initializedResponse = await postAction(
    page,
    encodeAuthoringAction(
      `m25-52-setup-init-${suffix}`,
      "initialize_timeline_history",
      {
        workspace_handle: authoringHandle,
        expected_reference_revision: authoringProjection!.reference.revision,
        expected_timeline_revision: authoringProjection!.timeline.revision,
        // IMPORTANT: keep every negotiated profile pin in this closed payload; omitting one makes
        // the supplied-host journey fail locally before it can exercise the real history route.
        authoring_schema: NLE_AUTHORING_SCHEMA,
        profile_id: NLE_AUTHORING_PROFILE_ID,
        operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
      },
    ),
  );
  expect(initializedResponse.status).toBe(200);
  // IMPORTANT: the negotiated operation profile is V2; decoding it as V1 rejects the real host
  // response before the waveform and playback assertions can observe product behavior.
  const initializedHistory = decodeTimelineHistoryProjectionV2(
    initializedResponse.body,
  );
  expect(initializedHistory.workspaceHandle).toBe(authoringHandle);
  expect(initializedHistory.rejection).toBeNull();

  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  const cleanupHostJourney = async () => {
    if ((await overlay.count()) > 0) {
      await overlay.locator('[data-h3-nle-action="close"]').click();
      await expect(overlay).toHaveCount(0);
    }
    const release = await postAction(
      page,
      encodeAuthoringAction(`m25-52-release-${suffix}`, "release_workspace", {
        workspace_handle: authoringHandle,
      }),
    );
    expect(release).toEqual({ status: 204, body: null });
    await expect.poll(() => activePlayback.size).toBe(0);
    await expect.poll(() => activeDecorations.size).toBe(0);
    await expect
      .poll(async () => {
        const receipt = await loadResourceReceipt();
        return m25WorkspaceResourcesReleased(
          receipt.current,
          receipt.retainedPackagedFonts,
          candidateFontBodies,
        );
      })
      .toBe(true);
    const resourceReceipt = await loadResourceReceipt();

    await page.evaluate(() => {
      const runtime = window as any;
      const app = runtime.comfyAPI.app.app;
      if (runtime.__h3M2552Anchor) app.graph.remove(runtime.__h3M2552Anchor);
      if (runtime.__h3M2552Native) app.graph.remove(runtime.__h3M2552Native);
      document.getElementById("h3-m25-52-host-container")?.remove();
    });
    const hostState = await page.evaluate(() => {
      const runtime = window as any;
      const app = runtime.comfyAPI.app.app;
      const baseline = runtime.__h3M2552HostBaseline;
      if (!baseline) throw new Error("host baseline is absent");
      const result = {
        workflowIdentityStable:
          app.extensionManager.workflow.activeWorkflow ===
          baseline.activeWorkflow,
        openWorkflowCountStable:
          app.extensionManager.workflow.openWorkflows.length ===
          baseline.openWorkflowCount,
        ownedMountCountStable:
          document.querySelectorAll("[data-h3-context-mount]").length ===
          baseline.ownedMountCount,
      };
      delete runtime.__h3M2552HostBaseline;
      return result;
    });
    const graphAfter = await captureM17CanonicalIdentity(page);
    const surroundings = diffGraphSurroundings({
      beforeValue: JSON.parse(graphBefore.graph),
      afterValue: JSON.parse(graphAfter.graph),
      reference: {
        ownedNodeIds: [],
        ownedLinkIds: [],
        anchorNodeId: "",
        ownedProjectionEqual: true,
      },
    });
    const queueAfter = await supportedHostQueueCounts(page);
    page.off("request", observeRequest);
    page.off("response", observeResponse);
    page.off("requestfailed", observeRequestFailed);
    expect(Object.values(hostState).every(Boolean)).toBe(true);
    expect(context.pages()).toHaveLength(pageCount);
    expect(queueAfter).toEqual(queueBefore);
    expect(surroundings.counts.owned).toBe(0);
    expect(promptCalls).toBe(0);
    expect(authoringCalls).toBeGreaterThanOrEqual(3);
    const networkEvidence = network.snapshot();
    expect(networkEvidence.interactionRemoteCount).toBe(0);
    expect(networkEvidence.interactionProviderCount).toBe(0);
    expectCandidateInteractionNetworkLocal(candidateNetwork);
    return { hostState, surroundings, resourceReceipt, networkEvidence };
  };
  await shell.locator('[data-h3-nle-entry="open"]').click();
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");
  await expect(overlay.locator("[data-h3-nle-asset]")).toHaveCount(12);
  const timelineStatus = overlay.locator('[data-h3-nle-status="timeline"]');
  // IMPORTANT: a newly initialized V2 history is intentionally empty and exposes only Undo/Redo.
  // Seed the first real clip before using zoom; reversing this order waits forever for a toolbar
  // that the empty-history surface must not render.
  await overlay
    .locator(
      '[data-h3-nle-asset="video_1"] [data-h3-nle-control="asset.insert"]',
    )
    .click();
  await expect.poll(() => timelineReceipts.length).toBe(1);
  expect(timelineRejections).toHaveLength(0);
  const primaryReceipt = timelineReceipts[0]!;
  await expect(timelineStatus).toHaveAttribute(
    "data-h3-transaction",
    primaryReceipt.transactionId,
  );
  await expect(timelineStatus).toHaveAttribute(
    "data-h3-nle-authoring",
    "ready",
  );
  const timelineScroll = overlay.locator(
    '[data-h3-nle-control="transport.scroll"]',
  );
  const setTimelineScroll = async (requestedFrame: number) => {
    const applied = await timelineScroll.evaluate((input, requested) => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )?.set;
      if (setter === undefined) throw new Error("range value setter is absent");
      setter.call(input, requested);
      const value = (input as HTMLInputElement).value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      return value;
    }, String(requestedFrame));
    await expect(timelineScroll).toHaveValue(applied);
    return applied;
  };
  const zoomIn = overlay.locator('[data-h3-nle-control="transport.zoom_in"]');
  for (let index = 0; index < 4; index += 1) await zoomIn.click();
  await expect
    .poll(async () => Number(await timelineScroll.getAttribute("max")))
    .toBeGreaterThan(0);
  await timelineScroll.focus();
  await page.keyboard.press("Home");

  const setupHistoryResponse = await postAction(
    page,
    encodeAuthoringAction(
      `m25-52-setup-read-${suffix}`,
      "read_timeline_history",
      { workspace_handle: authoringHandle },
    ),
  );
  expect(setupHistoryResponse.status).toBe(200);
  const setupHistory = decodeTimelineHistoryProjectionV2(
    setupHistoryResponse.body,
  );
  const lateAsset = setupHistory.authoring.assets.find(
    (asset) => asset.assetId === "video_3",
  );
  const setupPrimaryTrack = setupHistory.authoring.tracks.find(
    (track) => track.kind === "primary_video",
  );
  if (
    lateAsset?.sourceFrameCount === null ||
    lateAsset?.sourceFrameCount === undefined ||
    setupPrimaryTrack === undefined
  )
    throw new Error("late decoration setup is unavailable");
  // IMPORTANT: seed the distinct source only after the mounted timeline owns a narrowed start
  // viewport. The explicit refresh below accepts the new CAS before any later UI command; never
  // mutate behind an editor that still owns the older snapshot or remount briefly in fit mode.
  const lateDecorationFrame = 3_000;
  const lateTransaction = encodeTimelineTransactionV2({
    requestId: `m25-52-late-clip-${suffix}`,
    transactionId: `m25-52-late-clip-transaction-${suffix}`,
    workspaceHandle: setupHistory.workspaceHandle,
    expectedWorkspaceRevision: setupHistory.authoring.workspaceRevision,
    expectedTimelineRevision: setupHistory.authoring.timelineRevision,
    expectedTimelineFingerprint: setupHistory.authoring.timelineFingerprint,
    expectedAuthoringFingerprint: setupHistory.authoring.authoringFingerprint,
    commands: [
      {
        kind: "insert_asset_clip",
        payload: {
          clip: {
            clip_id: `m25-52-late-decoration-${suffix}`,
            asset_id: lateAsset.assetId,
            track_id: setupPrimaryTrack.trackId,
            start_frame: lateDecorationFrame,
            duration_frames: lateAsset.sourceFrameCount,
            source_start_frame: 0,
            enabled: true,
            transform: {
              anchor_x_bp: 5_000,
              anchor_y_bp: 5_000,
              position_x_bp: 0,
              position_y_bp: 0,
              scale_x_bp: 10_000,
              scale_y_bp: 10_000,
              rotation_mdeg: 0,
            },
            crop: { left_bp: 0, top_bp: 0, right_bp: 0, bottom_bp: 0 },
            opacity_bp: 10_000,
            blend: "normal",
            text: null,
            transition: { kind: "none", duration_frames: 0 },
            effect: {
              kind: "none",
              brightness_permille: 0,
              contrast_permille: 1_000,
              saturation_permille: 1_000,
            },
          },
        },
      },
    ],
  });
  const lateResponse = await postAction(
    page,
    encodeAuthoringAction(
      lateTransaction.request_id,
      "apply_timeline_transaction",
      lateTransaction,
    ),
  );
  expect(lateResponse.status).toBe(200);
  const lateReceipt = decodeTimelineReceiptV2(lateResponse.body);
  const lateDecorationClip = lateReceipt.authoring.clips.find(
    (clip) => clip.assetId === "video_3",
  );
  if (lateDecorationClip === undefined)
    throw new Error("late decoration clip is unavailable");
  expect(lateDecorationClip).toMatchObject({
    trackId: setupPrimaryTrack.trackId,
    startFrame: lateDecorationFrame,
  });
  await expect.poll(() => timelineReceipts.length).toBe(2);
  expect(timelineRejections).toHaveLength(0);

  const refreshAuthoring = shell.locator(
    '[data-h3-nle-action="refresh-authoring"]',
  );
  const callsBeforeRefresh = authoringCalls;
  await refreshAuthoring.evaluate((button) =>
    (button as HTMLButtonElement).click(),
  );
  await expect.poll(() => authoringCalls).toBeGreaterThan(callsBeforeRefresh);
  await expect(refreshAuthoring).toBeEnabled();
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");
  await expect(overlay.locator("[data-h3-nle-asset]")).toHaveCount(12);
  await expect
    .poll(async () => Number(await timelineScroll.getAttribute("max")))
    .toBeGreaterThan(0);
  await timelineScroll.focus();
  await page.keyboard.press("Home");

  if (process.env[SHORT_ROUTE_ENV] === "1") {
    const attachShortFailureFacts = async (
      stage: string,
      requestIndex: number,
      responseIndex: number,
    ) =>
      testInfo.attach("m25-52-short-route-failure-facts", {
        contentType: "application/json",
        body: JSON.stringify({
          stage,
          requests: leaseRequests.slice(requestIndex),
          responses: leaseResponses.slice(responseIndex),
          pendingDecorationCreates: pendingDecorationCreates.size,
          pendingDecorationOpens,
          activeDecorations: [...activeDecorations],
          resources: await loadResourceReceipt(),
        }),
      });
    // The diagnostic case isolates one normal waveform lifecycle. Keeping the Media-bin pane
    // mounted here would deliberately preempt peaks behind its twelve higher-priority thumbnails;
    // the full acceptance path below keeps that contention intact.
    const isolationRequestStart = leaseRequests.length;
    const isolationResponseStart = leaseResponses.length;
    try {
      await overlay.locator('[data-h3-nle-pane="text"]').click();
      await expect.poll(() => pendingDecorationCreates.size).toBe(0);
      await expect.poll(() => pendingDecorationOpens).toBe(0);
      await expect.poll(() => activeDecorations.size).toBe(0);
    } catch (error) {
      await attachShortFailureFacts(
        "isolate_media_bin",
        isolationRequestStart,
        isolationResponseStart,
      );
      throw error;
    }
    const requestStart = leaseRequests.length;
    const responseStart = leaseResponses.length;
    await setTimelineScroll(lateDecorationFrame);
    await expect(
      overlay.locator(`[data-h3-nle-clip="${lateDecorationClip.clipId}"]`),
    ).toHaveCount(1);
    try {
      await expect
        .poll(() =>
          leaseResponses
            .slice(responseStart)
            .some(
              ({ kind, operation, status }) =>
                kind === "audio_peaks" &&
                operation === "create" &&
                status === 200,
            ),
        )
        .toBe(true);
    } catch (error) {
      await attachShortFailureFacts(
        "await_audio_peaks_create",
        requestStart,
        responseStart,
      );
      throw error;
    }
    const create = leaseResponses
      .slice(responseStart)
      .find(
        ({ kind, operation, status }) =>
          kind === "audio_peaks" && operation === "create" && status === 200,
      )!;
    await expect
      .poll(() =>
        leaseResponses
          .slice(responseStart)
          .some(
            ({ ownerId, kind, operation, status }) =>
              ownerId === create.ownerId &&
              kind === "audio_peaks" &&
              operation === "open" &&
              status === 200,
          ),
      )
      .toBe(true);
    await expect
      .poll(() =>
        leaseResponses
          .slice(responseStart)
          .some(
            ({ ownerId, kind, operation, status }) =>
              ownerId === create.ownerId &&
              kind === "audio_peaks" &&
              operation === "release" &&
              status === 200,
          ),
      )
      .toBe(true);
    const successfulCreateIndex = leaseRequests.findIndex(
      ({ requestId }) => requestId === create.requestId,
    );
    expect(successfulCreateIndex).toBeGreaterThanOrEqual(requestStart);
    const lifecycle = leaseRequests
      .slice(successfulCreateIndex)
      .filter(({ ownerId }) => ownerId === create.ownerId);
    expect(lifecycle.map(({ operation }) => operation)).toEqual([
      "create",
      "open",
      "release",
    ]);
    expect(lifecycle.every(({ kind }) => kind === "audio_peaks")).toBe(true);
    expect(lifecycle.every(({ url }) => matchesM25LeaseRoute(url))).toBe(true);
    expect(lifecycle.every(({ endedAt }) => endedAt !== null)).toBe(true);
    await expect.poll(() => pendingDecorationCreates.size).toBe(0);
    await expect.poll(() => pendingDecorationOpens).toBe(0);
    const cleanup = await cleanupHostJourney();
    expect(
      cleanup.resourceReceipt.maximum.decorationLeases,
    ).toBeLessThanOrEqual(2);
    expect(cleanup.resourceReceipt.current).toEqual({
      leases: 0,
      cacheEntries: 0,
      cacheBytes: 0,
      activeReads: 0,
      decorationLeases: 0,
      videoLeases: 0,
    });
    await testInfo.attach("m25-52-short-real-route", {
      contentType: "application/json",
      body: JSON.stringify({
        schema: "h3.context.m25_52.short_real_route.v1",
        candidate: {
          bundleSha256: candidateBundle.sha256,
          installedInventorySha256: installedCandidateInventorySha256,
          backendInventorySha256: candidateBackendRuntime.inventorySha256,
        },
        ownerId: create.ownerId,
        kind: create.kind,
        lifecycle,
        responses: leaseResponses
          .slice(responseStart)
          .filter(({ ownerId }) => ownerId === create.ownerId),
        resources: cleanup.resourceReceipt,
        cleanup: {
          hostState: cleanup.hostState,
          surroundings: cleanup.surroundings,
        },
        network: cleanup.networkEvidence,
      }),
    });
    return;
  }

  let acceptedReceiptCount = timelineReceipts.length;
  const nextReceipt = async () => {
    const target = ++acceptedReceiptCount;
    const rejectionCount = timelineRejections.length;
    await expect
      .poll(
        () =>
          timelineReceipts.length === target ||
          timelineRejections.length > rejectionCount,
      )
      .toBe(true);
    if (timelineRejections.length > rejectionCount)
      throw new Error(
        `timeline transaction rejected: ${JSON.stringify(timelineRejections.at(-1))}`,
      );
    const receipt = timelineReceipts[target - 1]!;
    await expect(timelineStatus).toHaveAttribute(
      "data-h3-transaction",
      receipt.transactionId,
    );
    await expect(timelineStatus).toHaveAttribute(
      "data-h3-nle-authoring",
      "ready",
    );
    return receipt;
  };
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  const mediaAssets = primaryReceipt.authoring.assets.filter(
    (asset) => asset.kind !== "font",
  );
  const videos = primaryReceipt.authoring.assets.filter(
    (asset) => asset.kind === "video",
  );
  expect(mediaAssets).toHaveLength(12);
  expect(videos).toHaveLength(3);
  expect(
    videos.every(
      (asset) =>
        asset.embeddedAudio === "present_bound" &&
        asset.sourceFrameCount !== null &&
        asset.sourceSampleCount !== null,
    ),
  ).toBe(true);
  const primaryClip = primaryReceipt.authoring.clips.find(
    (clip) => clip.assetId === "video_1",
  );
  if (primaryClip === undefined || primaryClip.durationFrames < 48)
    throw new Error("primary mixed-load video is unavailable");
  const primaryTrack = primaryReceipt.authoring.tracks.find(
    (track) => track.kind === "primary_video",
  );
  if (primaryTrack === undefined)
    throw new Error("primary mixed-load track is unavailable");
  for (const [offset, kind] of [
    "video_overlay",
    "image_overlay",
    "text_overlay",
  ].entries()) {
    const trackMenu = await openTrackMenu(page, primaryTrack.trackId);
    await trackMenu.locator("select").selectOption(kind);
    await trackMenu
      .locator('input[type="number"]')
      .fill(String(primaryReceipt.authoring.tracks.length + offset));
    await trackMenu.locator('[data-h3-nle-control="track.add"]').click();
    const trackReceipt = await nextReceipt();
    expect(
      trackReceipt.authoring.tracks.find((track) => track.kind === kind)?.order,
    ).toBe(trackReceipt.authoring.tracks.length - 1);
  }
  const ruler = overlay.getByRole("slider", { name: "Playhead", exact: true });
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await seekPlayheadByKeyboard(page, ruler, 12);
  const overlayBase = timelineReceipts.at(-1)!;
  const overlayAsset = overlayBase.authoring.assets.find(
    (asset) => asset.assetId === "video_2",
  );
  const overlayTrack = overlayBase.authoring.tracks.find(
    (track) => track.kind === "video_overlay",
  );
  if (
    overlayAsset?.sourceFrameCount === null ||
    overlayAsset?.sourceFrameCount === undefined ||
    overlayTrack === undefined
  )
    throw new Error("mixed-load overlay setup is unavailable");
  // IMPORTANT: the ordinary asset action avoids overlap on the primary track and appends after
  // the distant demand clip. Pin this acceptance fixture to the real V2 route so the following
  // UI selection, split, transition and playback steps exercise an actual overlay at frame 12.
  const overlayTransaction = encodeTimelineTransactionV2({
    requestId: `m25-52-overlay-clip-${suffix}`,
    transactionId: `m25-52-overlay-clip-transaction-${suffix}`,
    workspaceHandle: overlayBase.workspaceHandle,
    expectedWorkspaceRevision: overlayBase.authoring.workspaceRevision,
    expectedTimelineRevision: overlayBase.authoring.timelineRevision,
    expectedTimelineFingerprint: overlayBase.authoring.timelineFingerprint,
    expectedAuthoringFingerprint: overlayBase.authoring.authoringFingerprint,
    commands: [
      {
        kind: "insert_asset_clip",
        payload: {
          clip: {
            clip_id: `m25-52-overlay-${suffix}`,
            asset_id: overlayAsset.assetId,
            track_id: overlayTrack.trackId,
            start_frame: 12,
            duration_frames: overlayAsset.sourceFrameCount,
            source_start_frame: 0,
            enabled: true,
            transform: {
              anchor_x_bp: 5_000,
              anchor_y_bp: 5_000,
              position_x_bp: 0,
              position_y_bp: 0,
              scale_x_bp: 10_000,
              scale_y_bp: 10_000,
              rotation_mdeg: 0,
            },
            crop: { left_bp: 0, top_bp: 0, right_bp: 0, bottom_bp: 0 },
            opacity_bp: 10_000,
            blend: "normal",
            text: null,
            transition: { kind: "none", duration_frames: 0 },
            effect: {
              kind: "none",
              brightness_permille: 0,
              contrast_permille: 1_000,
              saturation_permille: 1_000,
            },
          },
        },
      },
    ],
  });
  const overlayResponse = await postAction(
    page,
    encodeAuthoringAction(
      overlayTransaction.request_id,
      "apply_timeline_transaction",
      overlayTransaction,
    ),
  );
  expect(overlayResponse.status).toBe(200);
  const overlayReceipt = decodeTimelineReceiptV2(overlayResponse.body);
  await expect
    .poll(() => timelineReceipts.length)
    .toBe(acceptedReceiptCount + 1);
  acceptedReceiptCount += 1;
  expect(timelineRejections).toHaveLength(0);
  const callsBeforeOverlayRefresh = authoringCalls;
  await refreshAuthoring.evaluate((button) =>
    (button as HTMLButtonElement).click(),
  );
  await expect
    .poll(() => authoringCalls)
    .toBeGreaterThan(callsBeforeOverlayRefresh);
  await expect(timelineStatus).toHaveAttribute(
    "data-h3-nle-authoring",
    "ready",
  );
  await seekPlayheadByKeyboard(page, ruler, 12);
  const overlayClip = overlayReceipt.authoring.clips.find(
    (clip) => clip.assetId === "video_2",
  );
  if (overlayClip === undefined)
    throw new Error("cross-dissolve overlay video is unavailable");
  const selection = overlay.locator(
    `[data-h3-nle-clip="${overlayClip.clipId}"] [data-h3-nle-control="selection.set"]`,
  );
  await selection.click();
  await nextReceipt();
  await seekPlayheadByKeyboard(page, ruler, 24);
  await overlay.locator('[data-h3-nle-control="clip.split"]').click();
  const splitReceipt = await nextReceipt();
  expect(
    splitReceipt.authoring.clips.filter(
      (clip) =>
        clip.assetId === "video_2" && clip.trackId === overlayClip.trackId,
    ),
  ).toHaveLength(2);
  const inspector = overlay.locator(
    `[data-h3-nle-selected-clip="${overlayClip.clipId}"]`,
  );
  await inspector.locator('[data-h3-nle-property-tab="transition"]').click();
  await inspector
    .getByRole("combobox", { name: "Transition", exact: true })
    .selectOption("cross_dissolve_v1");
  await inspector
    .getByRole("spinbutton", { name: "Transition frames", exact: true })
    .fill("12");
  await inspector
    .locator('[data-h3-nle-control="boundary.transition"]')
    .click();
  const transitionReceipt = await nextReceipt();
  expect(
    transitionReceipt.authoring.clips.find(
      (clip) => clip.clipId === overlayClip.clipId,
    )?.transition,
  ).toEqual({ kind: "cross_dissolve_v1", durationFrames: 12 });
  const tracksById = new Map(
    transitionReceipt.authoring.tracks.map((track) => [track.trackId, track]),
  );
  const transitionVideoOwnerIds = transitionReceipt.authoring.clips
    .filter((clip) => {
      const track = tracksById.get(clip.trackId);
      return (
        clip.enabled &&
        track?.enabled === true &&
        ["primary_video", "video_overlay"].includes(track.kind) &&
        clip.startFrame <= 12 &&
        clip.startFrame + clip.durationFrames > 12
      );
    })
    .map((clip) => clip.clipId);
  expect(transitionVideoOwnerIds).toHaveLength(2);
  expect(transitionVideoOwnerIds).toContain(overlayClip.clipId);

  await seekPlayheadByKeyboard(page, ruler, 12);
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await overlay
    .locator(
      '[data-h3-nle-asset="first_frame_1"] [data-h3-nle-control="asset.insert"]',
    )
    .click();
  const imageReceipt = await nextReceipt();
  expect(
    imageReceipt.authoring.clips.some(
      (clip) => clip.assetId === "first_frame_1" && clip.startFrame === 12,
    ),
  ).toBe(true);
  await overlay.locator('[data-h3-nle-pane="text"]').click();
  await overlay.locator('[data-h3-nle-control="title.insert"]').click();
  const titleReceipt = await nextReceipt();
  expect(titleReceipt.authoring.clips).toHaveLength(6);
  expect(titleReceipt.authoring.clips.some((clip) => clip.text !== null)).toBe(
    true,
  );
  if (titleReceipt.renderSnapshot === null)
    throw new Error("mixed-load render snapshot is unavailable");
  const playbackFrames = Number(
    titleReceipt.renderSnapshot.output.durationFrames,
  );
  expect(Number.isSafeInteger(playbackFrames)).toBe(true);
  expect(playbackFrames).toBeGreaterThan(0);
  let idleFrame = -1;
  let overlapStage = "seek_idle_frame";
  let overlapTransitionFrame: number | null = null;
  let overlapPendingAtTransition = false;
  const attachFullFailureFacts = async () =>
    testInfo.attach("m25-52-full-route-failure-facts", {
      contentType: "application/json",
      body: JSON.stringify({
        schema: "h3.context.m25_52.full_route_failure_facts.v1",
        stage: overlapStage,
        idleFrame,
        mediaBinOverlapArmed,
        mediaBinTriggerRequest,
        overlapPlaybackTriggeredAt,
        overlapPendingAtPlaybackTrigger,
        overlapTransitionFrame,
        overlapPendingAtTransition,
        overlapVideoOwners,
        mediaBinOpenedAtFrame,
        mediaBinDemandUnfinishedAtTransition,
        mediaBinHttpPendingAtTransition,
        overlapCandidates,
        requests: leaseRequests,
        responses: leaseResponses,
        activePlayback: [...activePlayback],
        activeDecorations: [...activeDecorations],
        pendingDecorationCreates: pendingDecorationCreates.size,
        pendingDecorationOpens,
        maximumPlaybackOwners,
        authoring: {
          clips: titleReceipt.authoring.clips,
          tracks: titleReceipt.authoring.tracks,
        },
        resources: await loadResourceReceipt(),
      }),
    });
  try {
    const titleTracksById = new Map(
      titleReceipt.authoring.tracks.map((track) => [track.trackId, track]),
    );
    const enabledVideoClips = titleReceipt.authoring.clips
      .filter((clip) => {
        const track = titleTracksById.get(clip.trackId);
        return (
          clip.enabled &&
          track?.enabled === true &&
          ["primary_video", "video_overlay"].includes(track.kind)
        );
      })
      .sort((left, right) => left.startFrame - right.startFrame);
    idleFrame = 0;
    for (const clip of enabledVideoClips) {
      if (clip.startFrame > idleFrame) break;
      idleFrame = Math.max(idleFrame, clip.startFrame + clip.durationFrames);
    }
    expect(idleFrame).toBeLessThan(lateDecorationFrame);
    expect(
      enabledVideoClips.filter(
        (clip) =>
          clip.startFrame <= idleFrame &&
          clip.startFrame + clip.durationFrames > idleFrame,
      ),
    ).toHaveLength(0);
    await seekPlayheadByKeyboard(page, ruler, idleFrame);
    await expect.poll(() => activePlayback.size).toBe(0);
    await expect
      .poll(async () => (await loadResourceReceipt()).current.videoLeases)
      .toBe(0);
    await expect.poll(() => pendingDecorationCreates.size).toBe(0);
    await expect.poll(() => pendingDecorationOpens).toBe(0);
    await expect.poll(() => activeDecorations.size).toBe(0);
    await overlay.locator('[data-h3-nle-pane="text"]').click();
    const filmstripOpenBefore = operations.filmstrip?.open ?? 0;
    const peaksOpenBefore = operations.audio_peaks?.open ?? 0;
    await setTimelineScroll(lateDecorationFrame);
    await expect(
      overlay.locator(`[data-h3-nle-clip="${lateDecorationClip.clipId}"]`),
    ).toHaveCount(1);
    await expect
      .poll(() => operations.filmstrip?.open ?? 0)
      .toBeGreaterThan(filmstripOpenBefore);
    await expect
      .poll(() => operations.audio_peaks?.open ?? 0)
      .toBeGreaterThan(peaksOpenBefore);
    await expect.poll(() => pendingDecorationCreates.size).toBe(0);
    await expect.poll(() => pendingDecorationOpens).toBe(0);

    overlapStage = "prewarm_transition_owners";
    await seekPlayheadByKeyboard(page, ruler, 12);
    await expect
      .poll(() => [...activePlayback].sort())
      .toEqual([...transitionVideoOwnerIds].sort());
    await expect
      .poll(async () => (await loadResourceReceipt()).current.videoLeases)
      .toBe(2);

    overlapStage = "start_real_media_bin_demand";
    mediaBinOverlapArmed = true;
    await overlay.locator('[data-h3-nle-pane="assets"]').click();
    const assetBin = overlay.locator('[data-h3-nle-region="asset-bin"]');
    await expect(assetBin).toBeVisible();
    const pendingMediaBinCreate = () =>
      [...overlapCandidates]
        .reverse()
        .find(({ endedAt, outcome }) => endedAt === null && outcome === null);
    await expect.poll(pendingMediaBinCreate).toBeDefined();
    mediaBinTriggerRequest = pendingMediaBinCreate();
    if (mediaBinTriggerRequest === undefined)
      throw new Error("M25-52 lost its live Media-bin request before playback");
    overlapPendingAtPlaybackTrigger = !leaseRequests.some(
      ({ ownerId, operation, outcome, at }) =>
        ownerId === mediaBinTriggerRequest!.ownerId &&
        at >= mediaBinTriggerRequest!.at &&
        operation === "release" &&
        outcome === "response:200",
    );
    expect(overlapPendingAtPlaybackTrigger).toBe(true);
  } catch (error) {
    await attachFullFailureFacts();
    throw error;
  }
  const setupPlaybackHistory = playbackHistory.splice(0);
  const playbackOwnersAtStart = [...activePlayback].sort();
  maximumPlaybackOwners = activePlayback.size;

  const play = overlay.locator('[data-h3-nle-control="transport.play"]');
  await expect(play).toBeEnabled();
  await expect(timelineScroll).toBeEnabled();
  const timelineScrollMaximum = await timelineScroll.getAttribute("max");
  if (timelineScrollMaximum === null)
    throw new Error("timeline scroll maximum is unavailable");
  expect(Number(timelineScrollMaximum)).toBeGreaterThan(lateDecorationFrame);
  const compositionCanvas = overlay.locator(
    '[data-h3-nle-canvas="composition"]',
  );
  const readDecorationPresentation = () =>
    overlay
      .locator(`[data-h3-nle-clip="${lateDecorationClip.clipId}"]`)
      .evaluate((clip) => {
        const canvas = document.querySelector<HTMLCanvasElement>(
          '[data-h3-nle-canvas="timeline_decoration"]',
        )!;
        const canvasBounds = canvas.getBoundingClientRect();
        const clipBounds = clip.getBoundingClientRect();
        const sx = canvas.width / canvasBounds.width;
        const sy = canvas.height / canvasBounds.height;
        const left = Math.max(
          0,
          Math.floor((clipBounds.left - canvasBounds.left) * sx),
        );
        const right = Math.min(
          canvas.width,
          Math.ceil((clipBounds.right - canvasBounds.left) * sx),
        );
        const waveformTop = Math.max(
          0,
          Math.floor((clipBounds.bottom - canvasBounds.top - 28) * sy),
        );
        const bottom = Math.min(
          canvas.height,
          Math.ceil((clipBounds.bottom - canvasBounds.top - 3) * sy),
        );
        const filmstripTop = Math.max(
          0,
          Math.ceil((clipBounds.top - canvasBounds.top + 3) * sy),
        );
        const context2d = canvas.getContext("2d")!;
        const waveformPixels = context2d.getImageData(
          left,
          waveformTop,
          Math.max(1, right - left),
          Math.max(1, bottom - waveformTop),
        ).data;
        const filmstripPixels = context2d.getImageData(
          left,
          filmstripTop,
          Math.max(1, right - left),
          Math.max(1, waveformTop - filmstripTop),
        ).data;
        // M25-64 B-M2564-13: since M25-62 the waveform is stroked in the resolved
        // `--h3-nle-waveform` role colour, and a cyan rule counts none of it. A pixel is the
        // waveform's when every channel is within 16 levels of that colour, the tolerance
        // `m25_53ReferenceFidelity.spec.ts` measured and documents (WAVEFORM_COLOUR_TOLERANCE);
        // the same rule must match no filmstrip pixel, or it could be counting a source frame.
        const token = getComputedStyle(canvas)
          .getPropertyValue("--h3-nle-waveform")
          .trim();
        const scratch = document.createElement("canvas").getContext("2d")!;
        let hex: RegExpExecArray | null = null;
        if (CSS.supports("color", token)) {
          scratch.fillStyle = token;
          hex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(
            String(scratch.fillStyle),
          );
        }
        const rgb =
          hex === null
            ? null
            : [1, 2, 3].map((group) => Number.parseInt(hex[group]!, 16));
        const waveformColourPixels = (data: Uint8ClampedArray) => {
          let count = 0;
          if (rgb !== null)
            for (let index = 0; index < data.length; index += 4)
              if (
                data[index + 3]! > 0 &&
                Math.abs(data[index]! - rgb[0]!) <= 16 &&
                Math.abs(data[index + 1]! - rgb[1]!) <= 16 &&
                Math.abs(data[index + 2]! - rgb[2]!) <= 16
              )
                count += 1;
          return count;
        };
        let filmstripOpaque = 0;
        for (let index = 3; index < filmstripPixels.length; index += 4)
          if (filmstripPixels[index]! > 0) filmstripOpaque += 1;
        return {
          waveformColourResolved: rgb !== null,
          waveformColour: waveformColourPixels(waveformPixels),
          filmstripWaveformColour: waveformColourPixels(filmstripPixels),
          filmstripOpaque,
          canvasPointerEvents: getComputedStyle(canvas).pointerEvents,
        };
      });
  let transitionDecorationPresentation:
    Awaited<ReturnType<typeof readDecorationPresentation>> | undefined;
  let frameSamplePromise: Promise<number[]> | undefined;
  // IMPORTANT: canvas pixel readback is synchronous and can block the host long enough for a
  // time-based transport to jump frames. Verify the static waveform/filmstrip before playback so
  // the 12..23 presentation interval measures product rendering, not test-induced main-thread I/O.
  transitionDecorationPresentation = await readDecorationPresentation();
  expect(
    transitionDecorationPresentation.waveformColourResolved,
    "observer check: the waveform colour resolves to an opaque colour",
  ).toBe(true);
  expect(
    transitionDecorationPresentation.filmstripWaveformColour,
    "observer check: the waveform colour matches no filmstrip pixel",
  ).toBe(0);
  expect(transitionDecorationPresentation.waveformColour).toBeGreaterThan(0);
  expect(transitionDecorationPresentation.filmstripOpaque).toBeGreaterThan(0);
  overlapStage = "arm_media_bin";
  try {
    frameSamplePromise = collectCompleteFrameSamples(compositionCanvas, 24);
    overlapPlaybackTriggeredAt = performance.now();
    playbackStartedAt = overlapPlaybackTriggeredAt;
    await play.click();
    mediaBinOpenedAtFrame = Number(
      await compositionCanvas.getAttribute("data-h3-nle-presented-frame"),
    );
    expect(mediaBinOpenedAtFrame).toBeGreaterThanOrEqual(12);
    expect(mediaBinOpenedAtFrame).toBeLessThan(24);
    overlapStage = "cross_dissolve_with_queued_media_bin_demand";
    await expect
      .poll(
        async () => {
          const frame = Number(
            await compositionCanvas.getAttribute("data-h3-nle-presented-frame"),
          );
          const owners = [...activePlayback].sort();
          const demandUnfinished = !leaseRequests.some(
            ({ ownerId, operation, outcome, at }) =>
              ownerId === mediaBinTriggerRequest!.ownerId &&
              at >= mediaBinTriggerRequest!.at &&
              operation === "release" &&
              outcome === "response:200",
          );
          if (
            frame >= 12 &&
            frame < 24 &&
            demandUnfinished &&
            owners.length === transitionVideoOwnerIds.length &&
            owners.every(
              (ownerId, index) =>
                ownerId === [...transitionVideoOwnerIds].sort()[index],
            )
          ) {
            overlapTransitionFrame = frame;
            overlapVideoOwners = owners;
            overlapPendingAtTransition = true;
            mediaBinDemandUnfinishedAtTransition = demandUnfinished;
            mediaBinHttpPendingAtTransition =
              mediaBinTriggerRequest?.endedAt === null;
            return true;
          }
          return false;
        },
        { timeout: 1_000, intervals: [5, 10, 10, 10] },
      )
      .toBe(true);
    mediaBinOverlapArmed = false;
    if (mediaBinTriggerRequest === undefined)
      throw new Error("M25-52 had no real Media-bin request before playback");
    expect(mediaBinTriggerRequest).toMatchObject({
      operation: "create",
      kind: "thumbnail",
    });
    expect(matchesM25LeaseRoute(mediaBinTriggerRequest.url)).toBe(true);
    expect(mediaBinTriggerRequest.requestId).not.toBe("");
    expect(mediaBinTriggerRequest.ownerId).not.toBe("");
    expect(overlapPlaybackTriggeredAt).not.toBeNull();
    expect(overlapPendingAtTransition).toBe(true);
    expect(overlapVideoOwners).toEqual([...transitionVideoOwnerIds].sort());
    expect(maximumPlaybackOwners).toBeGreaterThanOrEqual(2);
  } catch (error) {
    await attachFullFailureFacts();
    throw error;
  }
  const frameSamples = await frameSamplePromise!;
  overlapStage = "release_playback_priority";
  await seekPlayheadByKeyboard(page, ruler, idleFrame);
  await expect.poll(() => activePlayback.size).toBe(0);
  await expect
    .poll(async () => (await loadResourceReceipt()).current.videoLeases)
    .toBe(0);
  playbackFinishedAt = performance.now();
  await expect.poll(() => mediaBinTriggerRequest!.endedAt).not.toBeNull();
  await expect
    .poll(
      () =>
        leaseRequests.some(
          ({ ownerId, operation, outcome, at }) =>
            ownerId === mediaBinTriggerRequest!.ownerId &&
            at >= mediaBinTriggerRequest!.at &&
            operation === "release" &&
            outcome === "response:200",
        ),
      {
        timeout: 30_000,
      },
    )
    .toBe(true);
  const completedMediaBinOwner = mediaBinTriggerRequest!.ownerId;
  const completedMediaBinLifecycle = leaseRequests.filter(
    ({ ownerId, at }) =>
      ownerId === completedMediaBinOwner && at >= mediaBinTriggerRequest!.at,
  );
  // IMPORTANT: playback priority ends when video acquisition settles, not when a presented
  // owner is finally released. Keep the owner-release timestamp as evidence, but do not make
  // decoration completion wait for the retained playback owner's lifetime.
  const lastPlaybackOwnerReleasedAt = Math.max(
    ...playbackHistory
      .filter(
        ({ operation, ownerId }) =>
          operation === "release" && transitionVideoOwnerIds.includes(ownerId),
      )
      .map(({ at }) => at),
  );
  expect(Number.isFinite(lastPlaybackOwnerReleasedAt)).toBe(true);
  const completedMediaBinCreate = completedMediaBinLifecycle.find(
    ({ kind, operation, outcome }) =>
      kind === "thumbnail" &&
      operation === "create" &&
      outcome === "response:200",
  );
  expect(completedMediaBinCreate).toBeDefined();
  expect(
    completedMediaBinLifecycle.some(
      ({ kind, operation, outcome }) =>
        kind === "thumbnail" &&
        operation === "open" &&
        outcome === "response:200",
    ),
  ).toBe(true);
  expect(
    completedMediaBinLifecycle.some(
      ({ kind, operation, outcome }) =>
        kind === "thumbnail" &&
        operation === "release" &&
        outcome === "response:200",
    ),
  ).toBe(true);
  const overlapAfterPlayback = {
    url: mediaBinTriggerRequest!.url,
    urlFacts: mediaBinTriggerRequest!.urlFacts,
    operation: mediaBinTriggerRequest!.operation,
    requestId: mediaBinTriggerRequest!.requestId,
    ownerId: mediaBinTriggerRequest!.ownerId,
    kind: mediaBinTriggerRequest!.kind,
    startedAt: mediaBinTriggerRequest!.at,
    playbackTriggeredAt: overlapPlaybackTriggeredAt,
    transitionFrame: overlapTransitionFrame,
    pendingAtPlaybackTrigger: overlapPendingAtPlaybackTrigger,
    pendingAtTransition: overlapPendingAtTransition,
    demandUnfinishedAtTransition: mediaBinDemandUnfinishedAtTransition,
    httpPendingAtTransition: mediaBinHttpPendingAtTransition,
    settledAt: mediaBinTriggerRequest!.endedAt,
    outcome: mediaBinTriggerRequest!.outcome,
    artificialResponseHold: false,
    lastPlaybackOwnerReleasedAt,
    completedOwnerId: completedMediaBinOwner,
    completedLifecycle: completedMediaBinLifecycle,
  };
  expect(["requestfailed", "response:200", "response:429"]).toContain(
    overlapAfterPlayback.outcome,
  );
  const overlappingDecorationRequest = {
    browser: overlapAfterPlayback,
    handler: mediaBinTriggerRequest!,
  };
  // IMPORTANT: the host can deliver a batch of consecutive presentation mutations before the
  // real pause click runs. Prove the complete 12..23 transition prefix instead of mistaking a
  // later, still-consecutive readback for a dropped frame or a failed pause.
  const transitionFrameSamples = frameSamples.filter(
    (frame) => frame >= 12 && frame < 24,
  );
  // IMPORTANT: reaching the endpoint can still skip intervening frames. Retain numeric
  // samples before either continuity assertion so that every dropped-frame failure is visible.
  await testInfo.attach("composition-presentation-frame-samples", {
    contentType: "application/json",
    body: JSON.stringify({
      schema: "h3.context.presentation_frame_samples.v1",
      frameSamples: frameSamples.slice(0, 1024),
    }),
  });
  if (transitionFrameSamples.at(-1) !== 23) {
    overlapStage = "incomplete_transition_sampling";
    await attachFullFailureFacts();
  }
  expect(transitionFrameSamples.length).toBeGreaterThan(1);
  expect(transitionFrameSamples[0]).toBe(12);
  expect(transitionFrameSamples.at(-1)).toBe(23);
  expect(
    transitionFrameSamples
      .slice(1)
      .every((frame, index) => frame === transitionFrameSamples[index]! + 1),
  ).toBe(true);
  expect(
    frameSamples
      .slice(1)
      .every((frame, index) => frame === frameSamples[index]! + 1),
  ).toBe(true);
  expect(maximumPlaybackOwners).toBeGreaterThanOrEqual(2);
  // Browser response arrival can briefly observe a replacement create before the older release.
  // The process-side resource receipt below is authoritative for the actual lease-pool ceiling.
  for (const ownerId of transitionVideoOwnerIds) {
    const acquiredDuringPlayback = playbackHistory.filter(
      (event) => event.operation === "create" && event.ownerId === ownerId,
    );
    expect(
      playbackOwnersAtStart.includes(ownerId) ||
        acquiredDuringPlayback.length === 1,
    ).toBe(true);
    expect(acquiredDuringPlayback.length).toBeLessThanOrEqual(1);
  }
  expect(maximumBrowserAcquisitions).toBeLessThanOrEqual(1);
  expect(overlappingDecorationRequest.handler.kind).toBe(
    overlappingDecorationRequest.browser.kind,
  );
  expect(overlappingDecorationRequest.handler.at).toBeGreaterThanOrEqual(
    overlappingDecorationRequest.browser.startedAt,
  );
  expect(overlappingDecorationRequest.browser.pendingAtTransition).toBe(true);
  expect(
    overlappingDecorationRequest.browser.demandUnfinishedAtTransition,
  ).toBe(true);

  await expect
    .poll(() => operations.audio_peaks?.create ?? 0)
    .toBeGreaterThan(0);
  await expect.poll(() => operations.audio_peaks?.open ?? 0).toBeGreaterThan(0);
  await expect
    .poll(() => operations.audio_peaks?.release ?? 0)
    .toBeGreaterThan(0);
  await expect.poll(() => operations.filmstrip?.open ?? 0).toBeGreaterThan(0);
  await expect.poll(() => pendingDecorationCreates.size).toBe(0);
  await expect.poll(() => pendingDecorationOpens).toBe(0);
  expect(decorationCompletions.some(({ kind }) => kind === "audio_peaks")).toBe(
    true,
  );
  const waveformPresentation = await readDecorationPresentation();
  expect(
    waveformPresentation.waveformColourResolved,
    "observer check: the waveform colour resolves to an opaque colour",
  ).toBe(true);
  expect(
    waveformPresentation.filmstripWaveformColour,
    "observer check: the waveform colour matches no filmstrip pixel",
  ).toBe(0);
  expect(waveformPresentation.waveformColour).toBeGreaterThan(0);
  expect(waveformPresentation.filmstripOpaque).toBeGreaterThan(0);
  expect(waveformPresentation.canvasPointerEvents).toBe("none");

  const timelineArea = overlay.locator('[data-h3-nle-area="timeline"]');
  await expect(timelineArea).toBeVisible();
  const viewport = page.viewportSize();
  if (viewport === null) throw new Error("reference viewport is unavailable");
  const timelineVisualBounds = await timelineArea.evaluate((area) => {
    const areaRect = area.getBoundingClientRect();
    const canvas = area.querySelector<HTMLCanvasElement>(
      '[data-h3-nle-canvas="timeline_decoration"]',
    );
    if (canvas === null)
      throw new Error("timeline decoration canvas is absent");
    const canvasRect = canvas.getBoundingClientRect();
    const style = getComputedStyle(canvas);
    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      deviceScaleFactor: window.devicePixelRatio,
      area: {
        x: areaRect.x,
        y: areaRect.y,
        width: areaRect.width,
        height: areaRect.height,
      },
      canvas: {
        x: canvasRect.x - areaRect.x,
        y: canvasRect.y - areaRect.y,
        width: canvasRect.width,
        height: canvasRect.height,
        display: style.display,
        visibility: style.visibility,
        opacity: Number(style.opacity),
        pointerEvents: style.pointerEvents,
      },
    };
  });
  expect(timelineVisualBounds.viewport).toEqual(viewport);
  expect(timelineVisualBounds.area.x).toBeGreaterThanOrEqual(0);
  expect(timelineVisualBounds.area.y).toBeGreaterThanOrEqual(0);
  expect(
    timelineVisualBounds.area.x + timelineVisualBounds.area.width,
  ).toBeLessThanOrEqual(viewport.width);
  expect(
    timelineVisualBounds.area.y + timelineVisualBounds.area.height,
  ).toBeLessThanOrEqual(viewport.height);
  expect(timelineVisualBounds.canvas.width).toBeGreaterThan(0);
  expect(timelineVisualBounds.canvas.height).toBeGreaterThan(0);
  expect(timelineVisualBounds.canvas.display).not.toBe("none");
  expect(timelineVisualBounds.canvas.visibility).toBe("visible");
  expect(timelineVisualBounds.canvas.opacity).toBeGreaterThan(0);

  const compositedTimelinePng = await timelineArea.screenshot({
    animations: "disabled",
  });
  const compositedTimelineCapture = {
    source: 'Playwright composed crop of [data-h3-nle-area="timeline"]',
    contentType: "image/png",
    sha256: createHash("sha256").update(compositedTimelinePng).digest("hex"),
    byteLength: compositedTimelinePng.byteLength,
    cropWidthPx: compositedTimelinePng.readUInt32BE(16),
    cropHeightPx: compositedTimelinePng.readUInt32BE(20),
    viewport,
    cssBounds: timelineVisualBounds.area,
    decorationCanvas: timelineVisualBounds.canvas,
    pngBase64: compositedTimelinePng.toString("base64"),
  };

  const cleanup = await cleanupHostJourney();
  const observedPlaybackOwnerIds = new Set([
    ...playbackOwnersAtStart,
    ...playbackHistory.map((event) => event.ownerId),
  ]);
  for (const ownerId of observedPlaybackOwnerIds) {
    const createCount = playbackHistory.filter(
      (event) => event.operation === "create" && event.ownerId === ownerId,
    ).length;
    const releaseCount = playbackHistory.filter(
      (event) => event.operation === "release" && event.ownerId === ownerId,
    ).length;
    expect(releaseCount).toBe(
      createCount + Number(playbackOwnersAtStart.includes(ownerId)),
    );
  }
  const { hostState, surroundings, resourceReceipt, networkEvidence } = cleanup;
  expect(resourceReceipt.maximum.decorationLeases).toBeLessThanOrEqual(2);
  // The global counter includes the accepted third prefetch slot; the transition
  // above still requires exactly two presented video owners.
  expect(resourceReceipt.maximum.videoLeases).toBeLessThanOrEqual(3);

  const evidenceJson = JSON.stringify({
    schema: "h3.context.m25_52.real_route_mixed_load.v1",
    candidate: {
      bundleSha256: candidateBundle.sha256,
      installedInventorySha256: installedCandidateInventorySha256,
      backendInventorySha256: candidateBackendRuntime.inventorySha256,
    },
    assets: {
      admitted: mediaAssets.length,
      videos: videos.length,
      embeddedAudio: videos.map((asset) => asset.embeddedAudio),
    },
    route: {
      operations,
      leaseRequests,
      leaseResponses,
      decorationRequests,
      decorationCompletions,
    },
    playback: {
      frameSamples,
      frameSampleCount: frameSamples.length,
      maximumPlaybackOwners,
      playbackOwnersAtStart,
      playbackHistory,
      setupPlaybackHistory,
      transitionVideoOwnerIds,
      overlappingDecorationRequest,
    },
    decorations: {
      maximumBrowserTrackedDecorations,
      maximumBrowserAcquisitions,
      lateDecorationClipId: lateDecorationClip.clipId,
      transitionDecorationPresentation,
      waveformPresentation,
      compositedTimelineCapture,
    },
    resources: resourceReceipt,
    cleanup: {
      hostState,
      surroundings,
      queueStable: true,
      pageCountStable: true,
    },
    network: networkEvidence,
  });
  expect(Buffer.byteLength(evidenceJson, "utf8")).toBeLessThan(240 * 1024);
  await testInfo.attach("m25-52-real-route-mixed-load", {
    contentType: "application/json",
    body: evidenceJson,
  });
});
