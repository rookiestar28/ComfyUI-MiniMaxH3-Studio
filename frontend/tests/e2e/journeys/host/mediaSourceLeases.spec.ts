import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { build } from "vite";

import {
  canonicalPublicRuntimeAssetFingerprint,
  type AuthoringMediaLeaseCreateRequest,
  type AuthoringMediaGeometry,
} from "../../../../src/contracts/authoringMediaLeaseCodec";
import type { PublicCompositionSnapshot } from "../../../../src/contracts/compositionCodec";
import {
  decodeTimelineHistoryProjection,
  decodeTimelineReceipt,
  encodeAuthoringAction,
  encodeTimelineTransaction,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import {
  build as buildCommand,
  freshIdentifier,
} from "../../../../src/components/nle/nleCommandBuilders";
import { buildPublicAssetManifest } from "../../../../src/runtime/publicAssetManifest";
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

const FIXTURE_PATH_ENV = "H3_CONTEXT_NLE_WORKSPACE_FIXTURE";
const FIXTURE_SCHEMA = "NleWorkspaceHostFixtureV1";

type WorkspaceFixture = Readonly<{
  schema: string;
  output: unknown;
  anchorId: string;
  promptId: string;
  qualification: Readonly<{
    schema: string;
    candidateBundleSha256: string;
  }>;
}>;

type LeaseFixture = Readonly<{
  create: AuthoringMediaLeaseCreateRequest;
  expectedAssetFingerprint: string;
  invalidateAction: unknown;
  releaseAction: unknown;
}>;

function record(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${name} is malformed`);
  return value as Record<string, unknown>;
}

async function loadWorkspaceFixture(): Promise<WorkspaceFixture> {
  const configured = process.env[FIXTURE_PATH_ENV]?.trim() ?? "";
  if (configured.length === 0)
    throw new Error(`${FIXTURE_PATH_ENV} is required`);
  const wire = record(
    JSON.parse(await readFile(resolve(repositoryRoot, configured), "utf8")),
    "workspace fixture",
  );
  if (wire.schema !== FIXTURE_SCHEMA)
    throw new Error("workspace fixture contract mismatch");
  const qualification = record(wire.qualification, "workspace qualification");
  if (
    qualification.schema !== "M25RealRuntimeQualificationV1" ||
    qualification.candidateBundleSha256 !== candidateBundle?.sha256
  )
    throw new Error("workspace fixture is not bound to the exact candidate");
  // IMPORTANT: accepted NLE workspace fixtures bind the owned graph anchor by stable string id;
  // narrowing it to a numeric LiteGraph id rejects the real M25-46 handoff before host execution.
  if (
    typeof wire.anchorId !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(wire.anchorId) ||
    typeof wire.promptId !== "string" ||
    wire.promptId.length === 0
  )
    throw new Error("workspace fixture identity is invalid");
  return wire as WorkspaceFixture;
}

function buildLeaseFixture(snapshot: PublicCompositionSnapshot): LeaseFixture {
  // IMPORTANT: the history codec already closes and camel-cases this snapshot. Re-decoding its
  // typed result as snake-case wire rejects the real route after consuming its one-way source claim.
  const manifest = buildPublicAssetManifest(snapshot);
  const asset = manifest.assets.find((candidate) => candidate.kind === "image");
  const clip = snapshot.clips.find(
    (candidate) => candidate.assetId === asset?.assetId,
  );
  if (asset?.kind !== "image" || clip === undefined)
    throw new Error("accepted workspace lacks one IMAGE clip");

  const actionScope = createHash("sha256")
    .update(snapshot.workspaceHandle)
    .digest("hex")
    .slice(0, 16);
  const create = Object.freeze({
    schema: "h3.context.authoring_media_lease.request.v1",
    operation: "create",
    requestId: "m25-13-host-primary",
    workspaceHandle: snapshot.workspaceHandle,
    workspaceRevision: snapshot.workspaceRevision,
    timelineRevision: snapshot.timelineRevision,
    publicFingerprint: snapshot.publicFingerprint,
    manifestFingerprint: manifest.manifestFingerprint,
    profileFingerprint: manifest.profileFingerprint,
    scope: "clip",
    clipId: clip.clipId,
    assetId: asset.assetId,
    derivativeKind: "image_proxy",
    ownerId: "m25-13-host-owner-a",
    runtimeEpoch: 1,
    sourceStartFrame: 0,
    sourceEndFrame: 1,
  }) satisfies AuthoringMediaLeaseCreateRequest;
  // IMPORTANT: Authoring action idempotency is process-global. Scope IDs to the ephemeral
  // workspace or consecutive supplied-host samples collide with an earlier workspace ledger.
  const invalidateRequestId = `m25-13-host-${actionScope}-invalidate`;
  const invalidateTransaction = encodeTimelineTransaction({
    requestId: invalidateRequestId,
    transactionId: `m25-13-host-${actionScope}-transaction`,
    workspaceHandle: snapshot.workspaceHandle,
    expectedWorkspaceRevision: snapshot.workspaceRevision,
    expectedTimelineRevision: snapshot.timelineRevision,
    expectedTimelineFingerprint: snapshot.timelineFingerprint,
    commands: [
      {
        kind: "set_clip_enabled",
        payload: { clip_id: clip.clipId, enabled: !clip.enabled },
      },
    ],
  });
  return Object.freeze({
    create,
    expectedAssetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
    invalidateAction: encodeAuthoringAction(
      invalidateRequestId,
      "apply_timeline_transaction",
      invalidateTransaction,
    ),
    releaseAction: encodeAuthoringAction(
      `m25-13-host-${actionScope}-release`,
      "release_workspace",
      { workspace_handle: snapshot.workspaceHandle },
    ),
  });
}

test("M25-48 preserves the existing real supplied-host clip lease row without queue or canvas drift", async ({
  context,
  page,
}, testInfo) => {
  test.setTimeout(90_000);
  test.skip(
    process.env.H3_CONTEXT_M25_48_HOST !== "1",
    "explicit M25-48 supplied-host sample required",
  );
  if (
    hostUrl === undefined ||
    !process.env.H3_CONTEXT_HOST_ROOT ||
    candidateBundle === null ||
    candidateBackendMode !== "exact" ||
    candidateBackendRuntime === null
  )
    throw new Error(
      "M25-48 requires an explicitly supplied host and exact frontend/backend candidate",
    );
  const workspaceFixture = await loadWorkspaceFixture();
  const built = await build({
    configFile: false,
    logLevel: "silent",
    build: {
      write: false,
      minify: false,
      lib: {
        entry: resolve(
          repositoryRoot,
          "frontend/src/host/authoringMediaSourceLease.ts",
        ),
        name: "H3MediaLease",
        formats: ["iife"],
      },
    },
  });
  const outputs = (Array.isArray(built) ? built : [built]).flatMap((result) =>
    "output" in result ? result.output : [],
  );
  if (outputs.length !== 1 || outputs[0]?.type !== "chunk")
    throw new Error("M25-13 lease client probe must be one in-memory script");
  const script = outputs[0].code;

  const allowedOrigin = new URL(hostUrl).origin;
  const network = monitorH3Network(page, allowedOrigin);
  const candidateNetwork = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  let promptCalls = 0;
  let leaseCalls = 0;
  let authoringCalls = 0;
  let authoringHandle: string | undefined;
  const authoringActions: string[] = [];
  const countRequests = (request: { method(): string; url(): string }) => {
    if (request.method() !== "POST") return;
    // IMPORTANT: ComfyUI may prefix extension routes with `/api`; normalize that host transport
    // spelling before attribution or real owned create/lease calls disappear from the evidence.
    const path = new URL(request.url()).pathname.replace(
      /^\/api(?=\/h3-context\/)/,
      "",
    );
    if (/\/(?:api\/)?prompt$/.test(path)) promptCalls += 1;
    if (path.startsWith("/h3-context/v1/authoring/media-source-leases"))
      leaseCalls += 1;
    if (path === "/h3-context/v1/authoring/action") {
      authoringCalls += 1;
      const action = (
        request as { postDataJSON?(): { action?: unknown } }
      ).postDataJSON?.().action;
      if (typeof action === "string") authoringActions.push(action);
    }
  };
  page.on("request", countRequests);
  page.on("response", async (response) => {
    const path = new URL(response.url()).pathname.replace(
      /^\/api(?=\/h3-context\/)/,
      "",
    );
    if (
      path !== "/h3-context/v1/authoring/action" ||
      response.status() !== 201 ||
      response.request().postDataJSON()?.action !== "create_authoring_workspace"
    )
      return;
    const body = record(await response.json(), "authoring workspace response");
    if (typeof body.workspace_handle === "string")
      authoringHandle = body.workspace_handle;
  });

  const injectionBefore = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injectionBefore);
  await beginSettledH3InteractionPhase(page, network, candidateNetwork);
  // IMPORTANT: capture host identity only after initialization settles. Taking this baseline at
  // registration observes the temporary empty workflow and reports drift caused before our lease.
  const graphBefore = await captureM17CanonicalIdentity(page);
  const pageCount = context.pages().length;
  const queueBefore = await supportedHostQueueCounts(page);
  await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: {
        app: {
          app: {
            extensionManager: {
              workflow: { activeWorkflow: unknown; openWorkflows: unknown[] };
              getSidebarTabs(): Array<{ id?: unknown }>;
            };
          };
        };
      };
      __h3M2513HostBaseline?: {
        activeWorkflow: unknown;
        openWorkflowCount: number;
        ownedSidebarCount: number;
        ownedMountCount: number;
      };
    };
    const app = runtime.comfyAPI.app.app;
    const workflow = app.extensionManager.workflow;
    runtime.__h3M2513HostBaseline = {
      activeWorkflow: workflow.activeWorkflow,
      openWorkflowCount: workflow.openWorkflows.length,
      ownedSidebarCount: app.extensionManager
        .getSidebarTabs()
        .filter((tab) => tab.id === "h3-context").length,
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
    panel.id = "h3-m25-48-lease-host-container";
    panel.className = "side-bar-panel sidebar-content-container";
    Object.assign(panel.style, {
      position: "fixed",
      inset: "80px auto 0 60px",
      width: "600px",
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
    runtime.__h3M2548LeaseAnchor = anchor;
    runtime.__h3M2548LeaseNative = native;
    runtime.comfyAPI.api.api.dispatchEvent(
      new CustomEvent("executed", {
        detail: { node: anchorId, prompt_id: promptId, output },
      }),
    );
  }, workspaceFixture);
  const shell = page.locator("#h3-m25-48-lease-host-container");
  await shell.locator('[data-page-id="production"]').click();
  await shell.locator('[data-h3-director-function="clip_editor"]').click();
  const start = shell.getByRole("button", {
    name: "Start authoring from this context",
    exact: true,
  });
  await start.click();
  await expect(start).toHaveCount(0);
  // IMPORTANT: the button disappears when creation enters loading, not when the workspace exists.
  // Opening the overlay before the 201 response skips its one-shot history initialization.
  await expect.poll(() => authoringHandle).not.toBeUndefined();
  await shell.locator('[data-h3-nle-entry="open"]').click();
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");
  // IMPORTANT: Media decoration and clip playback intentionally share one serial build owner.
  // A canvas exists before its bitmap is ready; only cleared loading status proves both decoration
  // leases published after release, avoiding a false-ready `busy` race without adding retries.
  await expect(
    overlay.locator("[data-h3-nle-asset] .h3-nle-thumbnail-status"),
  ).toHaveText(["", ""]);
  // Unmount the Media bin after proving its thumbnails so later clip qualification owns the
  // shared serial build window; keeping the pane live permits a demand refresh to race as `busy`.
  const textTab = overlay.locator('[data-h3-nle-pane="text"]');
  await textTab.click();
  await expect(textTab).toHaveAttribute("aria-selected", "true");
  await expect(overlay.locator("[data-h3-nle-asset]")).toHaveCount(0);
  const historyRequest = encodeAuthoringAction(
    `m25-48-host-history-${authoringHandle!.slice(-16)}`,
    "read_timeline_history",
    { workspace_handle: authoringHandle },
  );
  const historyResponse = await page.evaluate(async (payload) => {
    const response = await fetch("/h3-context/v1/authoring/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return { status: response.status, body: await response.json() };
  }, historyRequest);
  expect(historyResponse.status).toBe(200);
  const history = decodeTimelineHistoryProjection(historyResponse.body);
  expect(history.workspaceHandle).toBe(authoringHandle);
  expect(history.snapshot.clips).toHaveLength(0);
  const image = history.snapshot.assets.find((asset) => asset.kind === "image");
  if (image === undefined)
    throw new Error("accepted workspace lacks one IMAGE asset");
  const trackId = freshIdentifier(history.snapshot, "track");
  const clipId = freshIdentifier(history.snapshot, "clip");
  const setupRequestId = `m25-48-host-setup-${authoringHandle!.slice(-16)}`;
  // IMPORTANT: the migrated NLE fixture intentionally starts with only its legacy primary-video
  // track. Build the former IMAGE qualification clip through one accepted M25-46 transaction;
  // silently auto-creating a track in the Media card would weaken its explicit admission rule.
  const setupTransaction = encodeTimelineTransaction({
    requestId: setupRequestId,
    transactionId: `${setupRequestId}-transaction`,
    workspaceHandle: history.snapshot.workspaceHandle,
    expectedWorkspaceRevision: history.snapshot.workspaceRevision,
    expectedTimelineRevision: history.snapshot.timelineRevision,
    expectedTimelineFingerprint: history.snapshot.timelineFingerprint,
    commands: [
      buildCommand.createTrack(
        trackId,
        "image_overlay",
        history.snapshot.tracks.length,
      ),
      buildCommand.insertAssetClip({
        clipId,
        trackId,
        assetId: image.assetId,
        startFrame: 0,
        durationFrames: 1,
        sourceStartFrame: 0,
        text: null,
      }),
    ],
  });
  const setupResponse = await page.evaluate(
    async (payload) => {
      const response = await fetch("/h3-context/v1/authoring/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      return { status: response.status, body: await response.json() };
    },
    encodeAuthoringAction(
      setupRequestId,
      "apply_timeline_transaction",
      setupTransaction,
    ),
  );
  expect(setupResponse.status).toBe(200);
  const setup = decodeTimelineReceipt(setupResponse.body);
  expect(setup.snapshot.tracks).toContainEqual(
    expect.objectContaining({ trackId, kind: "image_overlay" }),
  );
  expect(setup.snapshot.clips).toContainEqual(
    expect.objectContaining({ clipId, trackId, assetId: image.assetId }),
  );
  const fixture = buildLeaseFixture(setup.snapshot);

  // Match the established capability probe: CDP evaluates the in-memory repository bundle
  // directly, avoiding an unsafe-eval dependency on the supplied host's CSP.
  await page.evaluate(
    `${script}\n;globalThis.__h3M2513LeaseModule = H3MediaLease;`,
  );
  const result = (await page.evaluate(async (leaseFixture) => {
    // Keep capability authority inside the real client's closure; neither this result nor
    // retained evidence exposes it.
    const module = (
      globalThis as typeof globalThis & {
        __h3M2513LeaseModule?: {
          createAuthoringMediaSourceLeaseClient(input: {
            fetchApi(route: string, init: RequestInit): Promise<Response>;
            requestId(): string;
          }): {
            create(
              request: AuthoringMediaLeaseCreateRequest,
              signal: AbortSignal,
              expectedAssetFingerprint: string,
            ): Promise<{
              state(): {
                revision: number;
                ownerId: string;
                runtimeEpoch: number;
                released: boolean;
              };
              open(signal: AbortSignal): Promise<{
                blob: Blob;
                geometry: AuthoringMediaGeometry | null;
              }>;
              renew(signal: AbortSignal): Promise<void>;
              transfer(
                ownerId: string,
                epoch: number,
                revoke: () => Promise<void>,
                signal: AbortSignal,
              ): Promise<void>;
              release(): Promise<void>;
            }>;
            close(): Promise<void>;
          };
          AuthoringMediaSourceLeaseError: new (...args: never[]) => Error & {
            disposition: string;
          };
        };
      }
    ).__h3M2513LeaseModule;
    if (module === undefined) throw new Error("lease client injection failed");
    let requestSequence = 0;
    const client = module.createAuthoringMediaSourceLeaseClient({
      fetchApi: (route, init) => fetch(route, init),
      requestId: () => `m25-13-host-command-${++requestSequence}`,
    });
    const disposition = (error: unknown) =>
      error instanceof module.AuthoringMediaSourceLeaseError
        ? error.disposition
        : "unexpected";
    const create = leaseFixture.create;
    const expected = leaseFixture.expectedAssetFingerprint;
    let workspaceReleased = false;
    const releaseWorkspace = async () => {
      const response = await fetch("/h3-context/v1/authoring/action", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(leaseFixture.releaseAction),
      });
      workspaceReleased = response.status === 204;
      await response.body?.cancel().catch(() => undefined);
      return response.status;
    };
    const decodePng = async (blob: Blob) => {
      if (blob.type !== "image/png") throw new Error("lease body is not PNG");
      const bitmap = await createImageBitmap(blob);
      try {
        if (
          bitmap.width < 1 ||
          bitmap.height < 1 ||
          bitmap.width > 8192 ||
          bitmap.height > 8192 ||
          bitmap.width * bitmap.height > 4_194_304
        )
          throw new Error("decoded PNG dimensions exceed the profile");
        return { width: bitmap.width, height: bitmap.height };
      } finally {
        bitmap.close();
      }
    };
    try {
      const current = await client.create(
        create,
        new AbortController().signal,
        expected,
      );
      const oldOwner = await client.create(
        create,
        new AbortController().signal,
        expected,
      );
      const firstBody = await current.open(new AbortController().signal);
      const firstPng = await decodePng(firstBody.blob);
      const firstGeometry = firstBody.geometry;
      await current.renew(new AbortController().signal);
      const renewedRevision = current.state().revision;
      let localRevocations = 0;
      await current.transfer(
        "m25-13-host-owner-b",
        2,
        async () => {
          localRevocations += 1;
        },
        new AbortController().signal,
      );
      const transferredRevision = current.state().revision;
      let oldOwnerDisposition = "unexpected-success";
      try {
        await oldOwner.open(new AbortController().signal);
      } catch (error) {
        oldOwnerDisposition = disposition(error);
      }
      const secondBody = await current.open(new AbortController().signal);
      const secondPng = await decodePng(secondBody.blob);
      const secondGeometry = secondBody.geometry;
      await current.release();

      const aborted = await client.create(
        {
          ...create,
          requestId: "m25-13-host-abort",
          ownerId: "m25-13-host-abort",
        },
        new AbortController().signal,
        expected,
      );
      const abortController = new AbortController();
      const opening = aborted.open(abortController.signal);
      abortController.abort();
      let abortDisposition = "unexpected-success";
      try {
        await opening;
      } catch (error) {
        abortDisposition = disposition(error);
      } finally {
        await aborted.release().catch(() => undefined);
      }

      const renewLease = await client.create(
        {
          ...create,
          requestId: "m25-13-host-stale-renew",
          ownerId: "m25-13-host-stale-renew",
        },
        new AbortController().signal,
        expected,
      );
      const openLease = await client.create(
        {
          ...create,
          requestId: "m25-13-host-stale-open",
          ownerId: "m25-13-host-stale-open",
        },
        new AbortController().signal,
        expected,
      );
      const mutation = await fetch("/h3-context/v1/authoring/action", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(leaseFixture.invalidateAction),
      });
      await mutation.body?.cancel().catch(() => undefined);
      if (mutation.status !== 200)
        throw new Error(
          `semantic fixture mutation was refused with status ${mutation.status}`,
        );
      let renewAfterMutation = "unexpected-success";
      try {
        await renewLease.renew(new AbortController().signal);
      } catch (error) {
        renewAfterMutation = disposition(error);
      }
      let openAfterMutation = "unexpected-success";
      try {
        await openLease.open(new AbortController().signal);
      } catch (error) {
        openAfterMutation = disposition(error);
      }
      await client.close();
      const workspaceReleaseStatus = await releaseWorkspace();
      let createAfterRelease = "unexpected-success";
      try {
        await client.create(
          {
            ...create,
            requestId: "m25-13-host-after-workspace-release",
            ownerId: "m25-13-host-after-workspace-release",
          },
          new AbortController().signal,
          expected,
        );
      } catch (error) {
        createAfterRelease = disposition(error);
      }
      return {
        firstPng,
        secondPng,
        firstGeometry,
        secondGeometry,
        renewedRevision,
        transferredRevision,
        localRevocations,
        oldOwnerDisposition,
        oldOwnerReleased: oldOwner.state().released,
        currentReleased: current.state().released,
        abortDisposition,
        abortReleased: aborted.state().released,
        renewAfterMutation,
        openAfterMutation,
        workspaceReleaseStatus,
        createAfterRelease,
      };
    } finally {
      await client.close().catch(() => undefined);
      if (!workspaceReleased) await releaseWorkspace().catch(() => undefined);
      delete (
        globalThis as typeof globalThis & {
          __h3M2513LeaseModule?: unknown;
        }
      ).__h3M2513LeaseModule;
    }
  }, fixture)) as {
    firstPng: { width: number; height: number };
    secondPng: { width: number; height: number };
    firstGeometry: AuthoringMediaGeometry | null;
    secondGeometry: AuthoringMediaGeometry | null;
    renewedRevision: number;
    transferredRevision: number;
    localRevocations: number;
    oldOwnerDisposition: string;
    oldOwnerReleased: boolean;
    currentReleased: boolean;
    abortDisposition: string;
    abortReleased: boolean;
    renewAfterMutation: string;
    openAfterMutation: string;
    workspaceReleaseStatus: number;
    createAfterRelease: string;
  };

  if (await overlay.count())
    await overlay.locator('[data-h3-nle-action="close"]').click();
  await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    if (runtime.__h3M2548LeaseAnchor)
      app.graph.remove(runtime.__h3M2548LeaseAnchor);
    if (runtime.__h3M2548LeaseNative)
      app.graph.remove(runtime.__h3M2548LeaseNative);
    document.getElementById("h3-m25-48-lease-host-container")?.remove();
  });

  const hostState = await page.evaluate(() => {
    const runtime = window as unknown as {
      comfyAPI: {
        app: {
          app: {
            extensionManager: {
              workflow: { activeWorkflow: unknown; openWorkflows: unknown[] };
              getSidebarTabs(): Array<{ id?: unknown }>;
            };
          };
        };
      };
      __h3M2513HostBaseline?: {
        activeWorkflow: unknown;
        openWorkflowCount: number;
        ownedSidebarCount: number;
        ownedMountCount: number;
      };
    };
    const app = runtime.comfyAPI.app.app;
    const baseline = runtime.__h3M2513HostBaseline;
    if (baseline === undefined) throw new Error("host baseline is absent");
    const workflow = app.extensionManager.workflow;
    return {
      workflowIdentityStable:
        workflow.activeWorkflow === baseline.activeWorkflow,
      openWorkflowCountStable:
        workflow.openWorkflows.length === baseline.openWorkflowCount,
      ownedSidebarCountStable:
        app.extensionManager
          .getSidebarTabs()
          .filter((tab) => tab.id === "h3-context").length ===
        baseline.ownedSidebarCount,
      ownedMountCountStable:
        document.querySelectorAll("[data-h3-context-mount]").length ===
        baseline.ownedMountCount,
    };
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
  page.off("request", countRequests);

  expect(result.renewedRevision).toBe(2);
  expect(result.transferredRevision).toBe(3);
  expect(result.localRevocations).toBe(1);
  expect(result.oldOwnerDisposition).toBe("lease_gone");
  expect(result.oldOwnerReleased).toBe(true);
  expect(result.currentReleased).toBe(true);
  expect(result.abortDisposition).toBe("cancelled");
  expect(result.abortReleased).toBe(true);
  expect(result.renewAfterMutation).toBe("lease_gone");
  expect(result.openAfterMutation).toBe("lease_gone");
  expect(result.workspaceReleaseStatus).toBe(204);
  expect(result.createAfterRelease).toBe("stale");
  expect(result.secondPng).toEqual(result.firstPng);
  expect(result.firstGeometry).toMatchObject({
    schema: "h3.authoring.media_geometry.v1",
    derivativeWidth: result.firstPng.width,
    derivativeHeight: result.firstPng.height,
  });
  expect(result.firstGeometry?.sourceWidth).toBeGreaterThan(0);
  expect(result.firstGeometry?.sourceHeight).toBeGreaterThan(0);
  expect(result.secondGeometry).toEqual(result.firstGeometry);
  const unstableHostKeys = Object.entries(hostState)
    .filter(([, stable]) => !stable)
    .map(([key]) => key);
  if (unstableHostKeys.length > 0)
    throw new Error(`host state changed: ${unstableHostKeys.join(",")}`);
  expect(context.pages()).toHaveLength(pageCount);
  expect(promptCalls).toBe(0);
  expect(authoringCalls).toBe(authoringActions.length);
  expect(
    authoringActions.filter(
      (action) => action === "create_authoring_workspace",
    ),
  ).toHaveLength(1);
  expect(
    authoringActions.filter(
      (action) => action === "apply_timeline_transaction",
    ),
  ).toHaveLength(2);
  expect(
    authoringActions.filter((action) => action === "release_workspace"),
  ).toHaveLength(1);
  expect(
    authoringActions.filter((action) => action === "read_timeline_history")
      .length,
  ).toBeGreaterThanOrEqual(1);
  expect(leaseCalls).toBeGreaterThan(0);
  expect(queueAfter).toEqual(queueBefore);
  expect(surroundings.counts.owned).toBe(0);
  const networkEvidence = network.snapshot();
  expect(networkEvidence.interactionRemoteCount).toBe(0);
  expect(networkEvidence.interactionProviderCount).toBe(0);
  expectCandidateInteractionNetworkLocal(candidateNetwork);

  await testInfo.attach("m25-48-existing-host-media-lease", {
    contentType: "application/json",
    body: JSON.stringify({
      schema: "h3.context.m25_48.existing_host_media_lease.v1",
      png: result.firstPng,
      geometry: result.firstGeometry,
      revisions: {
        renewed: result.renewedRevision,
        transferred: result.transferredRevision,
      },
      dispositions: {
        oldOwner: result.oldOwnerDisposition,
        abort: result.abortDisposition,
        renewAfterMutation: result.renewAfterMutation,
        openAfterMutation: result.openAfterMutation,
        createAfterRelease: result.createAfterRelease,
      },
      cleanup: {
        oldOwnerReleased: result.oldOwnerReleased,
        currentReleased: result.currentReleased,
        abortReleased: result.abortReleased,
        workspaceReleaseStatus: result.workspaceReleaseStatus,
      },
      host: {
        ...hostState,
        pageCountStable: true,
        promptCalls,
        authoringCalls,
        leaseCalls,
        queueStable: true,
        surroundings,
        network: networkEvidence,
      },
      candidate: {
        bundleSha256: candidateBundle.sha256,
        backendInventorySha256: candidateBackendRuntime.inventorySha256,
        clientProbeSha256: createHash("sha256").update(script).digest("hex"),
      },
    }),
  });
});

test("M25-49 paints and cleans a real supplied-host filmstrip through the shared lease owner", async ({
  page,
  context,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_M25_49_HOST !== "1",
    "explicit M25-49 supplied-host row required",
  );
  test.setTimeout(180_000);
  if (
    !hostUrl ||
    !process.env.H3_CONTEXT_HOST_ROOT ||
    candidateBundle === null ||
    candidateBackendMode !== "exact" ||
    candidateBackendRuntime === null
  )
    throw new Error(
      "M25-49 requires an explicitly supplied host and exact frontend/backend candidate",
    );
  const workspaceFixture = await loadWorkspaceFixture();
  await page.addInitScript(() => {
    const runtime = window as typeof window & {
      __h3M2549FilmstripBitmap?: {
        created: number;
        closed: number;
        live: number;
        maximumLive: number;
      };
    };
    const counters = {
      created: 0,
      closed: 0,
      live: 0,
      maximumLive: 0,
    };
    runtime.__h3M2549FilmstripBitmap = counters;
    const create = window.createImageBitmap.bind(window) as (
      ...arguments_: unknown[]
    ) => Promise<ImageBitmap>;
    (window as any).createImageBitmap = async (...arguments_: unknown[]) => {
      const bitmap = await create(...arguments_);
      const source = arguments_[0];
      if (source instanceof Blob && source.type === "image/jpeg") {
        counters.created += 1;
        counters.live += 1;
        counters.maximumLive = Math.max(counters.maximumLive, counters.live);
        const close = bitmap.close.bind(bitmap);
        let closed = false;
        Object.defineProperty(bitmap, "close", {
          value: () => {
            if (closed) return;
            closed = true;
            close();
            counters.closed += 1;
            counters.live -= 1;
          },
        });
      }
      return bitmap;
    };
  });

  const allowedOrigin = new URL(hostUrl).origin;
  const network = monitorH3Network(page, allowedOrigin);
  const candidateNetwork = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const filmstripOperations = { create: 0, open: 0, release: 0 };
  let promptCalls = 0;
  let authoringHandle: string | undefined;
  let authoringCalls = 0;
  const countRequests = (request: {
    method(): string;
    url(): string;
    postDataJSON?(): Record<string, unknown>;
  }) => {
    if (request.method() !== "POST") return;
    const path = new URL(request.url()).pathname.replace(
      /^\/api(?=\/h3-context\/)/,
      "",
    );
    if (/\/(?:api\/)?prompt$/.test(path)) promptCalls += 1;
    const body = request.postDataJSON?.() ?? {};
    if (path === "/h3-context/v1/authoring/action") authoringCalls += 1;
    if (
      path.startsWith("/h3-context/v1/authoring/media-source-leases") &&
      typeof body.ownerId === "string" &&
      body.ownerId.startsWith("nle-filmstrip-") &&
      (body.operation === "create" ||
        body.operation === "open" ||
        body.operation === "release")
    )
      filmstripOperations[body.operation] += 1;
  };
  page.on("request", countRequests);
  page.on("response", async (response) => {
    const path = new URL(response.url()).pathname.replace(
      /^\/api(?=\/h3-context\/)/,
      "",
    );
    if (
      path !== "/h3-context/v1/authoring/action" ||
      response.status() !== 201 ||
      response.request().postDataJSON()?.action !== "create_authoring_workspace"
    )
      return;
    const body = record(await response.json(), "authoring workspace response");
    if (typeof body.workspace_handle === "string")
      authoringHandle = body.workspace_handle;
  });

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
    runtime.__h3M2549HostBaseline = {
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
    panel.id = "h3-m25-49-filmstrip-host-container";
    panel.className = "side-bar-panel sidebar-content-container";
    Object.assign(panel.style, {
      position: "fixed",
      inset: "80px auto 0 60px",
      width: "600px",
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
    runtime.__h3M2549FilmstripAnchor = anchor;
    runtime.__h3M2549FilmstripNative = native;
    runtime.comfyAPI.api.api.dispatchEvent(
      new CustomEvent("executed", {
        detail: { node: anchorId, prompt_id: promptId, output },
      }),
    );
  }, workspaceFixture);

  const shell = page.locator("#h3-m25-49-filmstrip-host-container");
  await shell.locator('[data-page-id="production"]').click();
  await shell.locator('[data-h3-director-function="clip_editor"]').click();
  const start = shell.getByRole("button", {
    name: "Start authoring from this context",
    exact: true,
  });
  await start.click();
  await expect(start).toHaveCount(0);
  await expect.poll(() => authoringHandle).not.toBeUndefined();
  await shell.locator('[data-h3-nle-entry="open"]').click();
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");
  await expect(
    overlay.locator("[data-h3-nle-asset] .h3-nle-thumbnail-status"),
  ).toHaveText(["", ""]);

  const videoCard = overlay.locator('[data-h3-nle-card-index="2"]');
  await expect(videoCard).toHaveAttribute("data-h3-nle-asset", "video_1");
  await videoCard.locator('[data-h3-nle-control="asset.insert"]').click();
  await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);
  const ruler = overlay.locator('[data-h3-nle-control="transport.seek"]');
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await ruler.press("End");
  const maximumFrame = await ruler.getAttribute("aria-valuemax");
  if (maximumFrame === null)
    throw new Error("timeline maximum frame is absent");
  await expect(ruler).toHaveAttribute("aria-valuenow", maximumFrame);
  await expect
    .poll(() => filmstripOperations)
    .toMatchObject({ create: 1, open: 1, release: 1 });
  await expect
    .poll(() => page.evaluate(() => (window as any).__h3M2549FilmstripBitmap))
    .toMatchObject({ created: 1, closed: 0, live: 1, maximumLive: 1 });

  const clipBody = overlay.locator(".h3-nle-clip-body").first();
  const canvas = overlay.locator('[data-h3-nle-canvas="timeline_decoration"]');
  const presentation = await clipBody.evaluate((body) => {
    const canvas = document.querySelector<HTMLCanvasElement>(
      '[data-h3-nle-canvas="timeline_decoration"]',
    )!;
    const clip = body.closest<HTMLElement>("[data-h3-nle-clip]")!;
    const canvasBounds = canvas.getBoundingClientRect();
    const clipBounds = clip.getBoundingClientRect();
    const x = Math.max(
      0,
      Math.min(
        canvas.width - 1,
        Math.floor(clipBounds.left - canvasBounds.left + 8),
      ),
    );
    const y = Math.max(
      0,
      Math.min(
        canvas.height - 1,
        Math.floor(clipBounds.top - canvasBounds.top + 12),
      ),
    );
    const pixel = [...canvas.getContext("2d")!.getImageData(x, y, 1, 1).data];
    const top = document.elementFromPoint(
      clipBounds.left + clipBounds.width / 2,
      clipBounds.top + clipBounds.height / 2,
    );
    return {
      pixel,
      bodyBackground: getComputedStyle(body).backgroundColor,
      bodyZ: getComputedStyle(body).zIndex,
      canvasZ: getComputedStyle(canvas).zIndex,
      canvasPointerEvents: getComputedStyle(canvas).pointerEvents,
      bodyOwnsHit: top === body || body.contains(top),
    };
  });
  expect(presentation.pixel[3]).toBeGreaterThan(0);
  expect(presentation).toMatchObject({
    bodyBackground: "rgba(0, 0, 0, 0)",
    bodyZ: "5",
    canvasZ: "4",
    canvasPointerEvents: "none",
    bodyOwnsHit: true,
  });

  await overlay.locator('[data-h3-nle-action="close"]').click();
  await expect(overlay).toHaveCount(0);
  await expect
    .poll(() => page.evaluate(() => (window as any).__h3M2549FilmstripBitmap))
    .toMatchObject({ created: 1, closed: 1, live: 0, maximumLive: 1 });
  const releaseStatus = await page.evaluate(
    async (payload) => {
      const response = await fetch("/h3-context/v1/authoring/action", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      await response.body?.cancel().catch(() => undefined);
      return response.status;
    },
    encodeAuthoringAction(
      `m25-49-host-release-${authoringHandle!.slice(-16)}`,
      "release_workspace",
      { workspace_handle: authoringHandle },
    ),
  );
  expect(releaseStatus).toBe(204);

  await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    if (runtime.__h3M2549FilmstripAnchor)
      app.graph.remove(runtime.__h3M2549FilmstripAnchor);
    if (runtime.__h3M2549FilmstripNative)
      app.graph.remove(runtime.__h3M2549FilmstripNative);
    document.getElementById("h3-m25-49-filmstrip-host-container")?.remove();
  });
  const hostState = await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const baseline = runtime.__h3M2549HostBaseline;
    if (baseline === undefined) throw new Error("host baseline is absent");
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
    delete runtime.__h3M2549HostBaseline;
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
  page.off("request", countRequests);
  // IMPORTANT: the supplied canvas is shared and mutates foreign workflow serialization while
  // owned nodes are added and removed. Assert the stable host identity and owned projection;
  // whole-workflow equality rejects valid cleanup on real ComfyUI hosts.
  const unstableHostKeys = Object.entries(hostState)
    .filter(([, stable]) => !stable)
    .map(([key]) => key);
  if (unstableHostKeys.length > 0)
    throw new Error(`host state changed: ${unstableHostKeys.join(",")}`);
  expect(context.pages()).toHaveLength(pageCount);
  expect(queueAfter).toEqual(queueBefore);
  expect(surroundings.counts.owned).toBe(0);
  expect(promptCalls).toBe(0);
  expect(authoringCalls).toBeGreaterThanOrEqual(3);
  const networkEvidence = network.snapshot();
  expect(networkEvidence.interactionRemoteCount).toBe(0);
  expect(networkEvidence.interactionProviderCount).toBe(0);
  expectCandidateInteractionNetworkLocal(candidateNetwork);

  await testInfo.attach("m25-49-real-host-filmstrip", {
    contentType: "application/json",
    body: JSON.stringify({
      schema: "h3.context.m25_49.real_host_filmstrip.v1",
      operations: filmstripOperations,
      presentation: {
        pixelPresent: presentation.pixel[3]! > 0,
        bodyBackground: presentation.bodyBackground,
        bodyZ: presentation.bodyZ,
        canvasZ: presentation.canvasZ,
        canvasPointerEvents: presentation.canvasPointerEvents,
        bodyOwnsHit: presentation.bodyOwnsHit,
      },
      cleanup: {
        releaseStatus,
        bitmap: await page.evaluate(
          () => (window as any).__h3M2549FilmstripBitmap,
        ),
      },
      host: {
        pageCountStable: true,
        queueStable: true,
        surroundings,
        network: networkEvidence,
      },
      candidate: {
        bundleSha256: candidateBundle.sha256,
        backendInventorySha256: candidateBackendRuntime.inventorySha256,
      },
    }),
  });
});
