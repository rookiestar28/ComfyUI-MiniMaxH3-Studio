import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import type { Locator, Request } from "@playwright/test";

import {
  decodeAuthoringProjection,
  decodeTimelineHistoryProjectionV2,
  decodeTimelineReceiptV2,
  encodeAuthoringAction,
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
  type TimelineReceiptV2,
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
import { expect, test } from "../../host/fixture";
import { supportedHostQueueCounts } from "../../host/managed";
import {
  beginSettledH3InteractionPhase,
  expectCandidateInteractionNetworkLocal,
  monitorCandidateInitiatorNetwork,
  monitorH3Network,
  waitForH3Registration,
} from "../../host/network";

const FIXTURE_ENV = "H3_CONTEXT_NLE_WORKSPACE_FIXTURE";
const INVENTORY_ENV = "H3_CONTEXT_CANDIDATE_INVENTORY_SHA256";
const CONTAINER_ID = "h3-content-extent-host-container";
const SOURCE_FRAMES = 96;

type WorkspaceFixture = Readonly<{
  schema: "NleWorkspaceHostFixtureV1";
  output: unknown;
  anchorId: string;
  promptId: string;
  qualification: Readonly<{
    schema: "M25RealRuntimeQualificationV1";
    normalStartupMediaActive: boolean;
    activeModulesInstalledOnly: boolean;
    backupModulesExcluded: boolean;
    canonicalNodesRegistered: boolean;
    candidateBundleSha256: string;
  }>;
}>;

function authoringAction(request: Request): string | null {
  const path = new URL(request.url()).pathname.replace(
    /^\/api(?=\/h3-context\/)/,
    "",
  );
  if (path !== "/h3-context/v1/authoring/action" || request.method() !== "POST")
    return null;
  try {
    const action = request.postDataJSON()?.action;
    return typeof action === "string" ? action : null;
  } catch {
    return null;
  }
}

async function expectExtent(
  overlay: Locator,
  receipt: TimelineReceiptV2 | null,
  frames: number,
): Promise<void> {
  if (frames === 0) {
    expect(receipt?.authoring.contentEndExclusive ?? 0).toBe(0);
    expect(receipt?.renderSnapshot ?? null).toBeNull();
    await expect(overlay.getByRole("slider", { name: "Playhead" })).toHaveCount(
      0,
    );
    await expect(
      overlay.locator('[data-h3-nle-region="timeline"]'),
    ).toContainText("Edit capacity 00:02:30:00");
    return;
  }
  if (receipt === null || receipt.renderSnapshot === null)
    throw new Error("positive content extent has no accepted render snapshot");
  expect(receipt.authoring.contentEndExclusive).toBe(frames);
  expect(receipt.renderSnapshot.output.durationFrames).toBe(frames);
  await expect(
    overlay.getByRole("slider", { name: "Playhead" }),
  ).toHaveAttribute("aria-valuemax", String(frames - 1));
}

test.use({ viewport: { width: 1402, height: 868 }, deviceScaleFactor: 1 });

test("supplied host V2 content extent follows accepted placements", async ({
  page,
  context,
}, testInfo) => {
  const verifyContentNavigation = process.env.H3_CONTEXT_M25_59_HOST === "1";
  test.skip(
    process.env.H3_CONTEXT_M25_57_HOST !== "1" && !verifyContentNavigation,
    "explicit supplied-host content-extent or navigation row required",
  );
  test.setTimeout(180_000);
  const installedInventorySha256 = process.env[INVENTORY_ENV];
  if (
    !hostUrl ||
    candidateBundle === null ||
    candidateBackendMode !== "exact" ||
    candidateBackendRuntime === null ||
    installedInventorySha256 === undefined ||
    !/^[0-9a-f]{64}$/.test(installedInventorySha256)
  )
    throw new Error("an exact supplied-host candidate is required");

  const configuredFixture = process.env[FIXTURE_ENV]?.trim();
  if (!configuredFixture) throw new Error("owned host fixture is required");
  const fixture = JSON.parse(
    await readFile(resolve(repositoryRoot, configuredFixture), "utf8"),
  ) as WorkspaceFixture;
  if (fixture.schema !== "NleWorkspaceHostFixtureV1")
    throw new Error("host fixture contract mismatch");
  expect(fixture.qualification).toMatchObject({
    schema: "M25RealRuntimeQualificationV1",
    normalStartupMediaActive: true,
    activeModulesInstalledOnly: true,
    backupModulesExcluded: true,
    canonicalNodesRegistered: true,
    candidateBundleSha256: candidateBundle.sha256,
  });

  const allowedOrigin = new URL(hostUrl).origin;
  const network = monitorH3Network(page, allowedOrigin);
  const candidateNetwork = await monitorCandidateInitiatorNetwork(
    context,
    page,
    allowedOrigin,
  );
  const actions: string[] = [];
  let promptRequests = 0;
  let authoringHandle: string | undefined;
  page.on("request", (request) => {
    const action = authoringAction(request);
    if (action !== null) actions.push(action);
    if (
      request.method() === "POST" &&
      /\/(?:api\/)?prompt$/.test(new URL(request.url()).pathname)
    )
      promptRequests += 1;
  });

  const injectionBefore = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injectionBefore);
  await beginSettledH3InteractionPhase(page, network, candidateNetwork);
  const queueBefore = await supportedHostQueueCounts(page);
  const pageCount = context.pages().length;
  const graphBefore = await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    runtime.__h3ContentExtentBaseline = {
      activeWorkflow: app.extensionManager.workflow.activeWorkflow,
      openWorkflowCount: app.extensionManager.workflow.openWorkflows.length,
      ownedMountCount: document.querySelectorAll("[data-h3-context-mount]")
        .length,
    };
    return structuredClone(app.graph.serialize());
  });

  await page.evaluate(
    ({ output, anchorId, promptId, containerId }) => {
      const runtime = window as any;
      const app = runtime.comfyAPI.app.app;
      const tab = app.extensionManager
        .getSidebarTabs()
        .find((entry: any) => entry.id === "h3-context");
      if (!tab) throw new Error("Context sidebar registration is unavailable");
      const panel = document.createElement("div");
      panel.id = containerId;
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
      if (!native)
        throw new Error("supplied native H3 registration unavailable");
      anchor.id = anchorId;
      app.graph.add(anchor);
      app.graph.add(native);
      runtime.__h3ContentExtentAnchor = anchor;
      runtime.__h3ContentExtentNative = native;
      runtime.comfyAPI.api.api.dispatchEvent(
        new CustomEvent("executed", {
          detail: { node: anchorId, prompt_id: promptId, output },
        }),
      );
    },
    { ...fixture, containerId: CONTAINER_ID },
  );

  const shell = page.locator(`#${CONTAINER_ID}`);
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  try {
    await expect(page.locator("#splash-loader")).toBeHidden();
    await shell.locator('[data-page-id="production"]').click();
    await shell.locator('[data-h3-director-function="clip_editor"]').click();
    const start = shell.getByRole("button", {
      name: "Start authoring from this context",
      exact: true,
    });
    const createdResponse = page.waitForResponse(
      (response) =>
        authoringAction(response.request()) === "create_authoring_workspace",
    );
    await start.click();
    const created = await createdResponse;
    expect(created.status()).toBe(201);
    const authoring = decodeAuthoringProjection(await created.json());
    authoringHandle = authoring.workspaceHandle;

    const initializationPayload = encodeAuthoringAction(
      `content-extent-init-${authoringHandle.slice(-16)}`,
      "initialize_timeline_history",
      {
        workspace_handle: authoringHandle,
        expected_reference_revision: authoring.reference.revision,
        expected_timeline_revision: authoring.timeline.revision,
        authoring_schema: NLE_AUTHORING_SCHEMA,
        profile_id: NLE_AUTHORING_PROFILE_ID,
        operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
      },
    );
    const initialized = await page.evaluate(async (payload) => {
      const response = await fetch("/h3-context/v1/authoring/action", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      return { status: response.status, body: await response.json() };
    }, initializationPayload);
    expect(initialized.status, JSON.stringify(initialized.body)).toBe(200);
    const initialHistory = decodeTimelineHistoryProjectionV2(initialized.body);
    expect(initialHistory.rejection).toBeNull();
    expect(initialHistory.authoring.contentEndExclusive).toBe(0);
    expect(initialHistory.renderSnapshot).toBeNull();

    await shell.locator('[data-h3-nle-entry="open"]').click();
    await expect(overlay).toBeVisible();
    await expect(
      overlay.locator('[data-h3-nle-status="timeline"]'),
    ).toHaveAttribute("data-h3-nle-authoring", "ready");
    await expectExtent(overlay, null, 0);

    const videoAssets = initialHistory.authoring.assets.filter(
      (asset) =>
        asset.kind === "video" && asset.sourceFrameCount === SOURCE_FRAMES,
    );
    expect(videoAssets.length).toBeGreaterThanOrEqual(2);
    let transactionOrdinal = 0;
    const receipts: TimelineReceiptV2[] = [];
    const transact = async (
      action: () => Promise<void>,
      expectedKinds: readonly string[],
    ): Promise<TimelineReceiptV2> => {
      const responsePromise = page.waitForResponse(
        (response) =>
          authoringAction(response.request()) === "apply_timeline_transaction",
      );
      await action();
      const response = await responsePromise;
      expect(response.status()).toBe(200);
      const request = response.request().postDataJSON();
      expect(request.payload.commands.map((row: any) => row.kind)).toEqual(
        expectedKinds,
      );
      const receipt = decodeTimelineReceiptV2(await response.json());
      receipts.push(receipt);
      transactionOrdinal += 1;
      await expect(
        overlay.locator('[data-h3-nle-status="timeline"]'),
      ).toHaveAttribute("data-h3-transaction", receipt.transactionId);
      return receipt;
    };
    const add = (assetId: string) =>
      transact(
        () =>
          overlay
            .locator(`[data-h3-nle-asset="${assetId}"] .h3-nle-media-primary`)
            .click(),
        ["insert_asset_clip"],
      );
    const undo = () =>
      transact(
        () => overlay.locator('[data-h3-nle-control="history.undo"]').click(),
        ["undo"],
      );
    const redo = () =>
      transact(
        () => overlay.locator('[data-h3-nle-control="history.redo"]').click(),
        ["redo"],
      );
    const remove = async (receipt: TimelineReceiptV2, index: number) => {
      const clip = receipt.authoring.clips[index];
      if (!clip) throw new Error("accepted clip is unavailable for deletion");
      const selected = await transact(
        () =>
          overlay
            .locator(
              `[data-h3-nle-clip="${clip.clipId}"] [data-h3-nle-control="selection.set"]`,
            )
            .click(),
        ["select_clips"],
      );
      expect(selected.selection).toEqual([clip.clipId]);
      return transact(
        () => overlay.locator('[data-h3-nle-control="clip.remove"]').click(),
        ["remove_clip"],
      );
    };

    const first = await add(videoAssets[0]!.assetId);
    await expectExtent(overlay, first, SOURCE_FRAMES);
    expect(first.authoring.clips).toHaveLength(1);
    expect(first.authoring.clips[0]).toMatchObject({
      startFrame: 0,
      durationFrames: SOURCE_FRAMES,
      sourceStartFrame: 0,
    });
    const playhead = overlay.getByRole("slider", { name: "Playhead" });
    await playhead.focus();
    await playhead.press("Home");
    await expect(playhead).toHaveAttribute("aria-valuenow", "0");

    const second = await add(videoAssets[1]!.assetId);
    await expectExtent(overlay, second, SOURCE_FRAMES * 2);
    expect(second.authoring.clips.map((clip) => clip.startFrame)).toEqual([
      0,
      SOURCE_FRAMES,
    ]);

    let contentNavigationEvidence: Record<string, unknown> | null = null;
    if (verifyContentNavigation) {
      const timeline = overlay.locator('[data-h3-nle-region="timeline"]');
      const fit = overlay.locator('[data-h3-nle-control="transport.zoom_fit"]');
      const grid = overlay.getByRole("grid");
      const lane = overlay.locator(
        '[data-h3-nle-track][data-kind="primary_video"] .h3-nle-track-lane',
      );
      const lastClip = overlay.locator(
        `[data-h3-nle-clip="${second.authoring.clips.at(-1)!.clipId}"]`,
      );
      await fit.click();
      const [laneBox, clipBox, gridBox] = await Promise.all([
        lane.boundingBox(),
        lastClip.boundingBox(),
        grid.boundingBox(),
      ]);
      if (laneBox === null || clipBox === null || gridBox === null)
        throw new Error("supplied-host timeline geometry is unavailable");
      const trailingPadding =
        laneBox.x + laneBox.width - (clipBox.x + clipBox.width);
      expect(trailingPadding).toBeCloseTo(24, 0);
      await expect(timeline).toHaveAttribute("data-h3-nle-view-start", "0");

      await page.mouse.move(
        gridBox.x + gridBox.width * 0.65,
        gridBox.y + gridBox.height * 0.5,
      );
      const beforeZoom = await timeline.evaluate((element) => ({
        scale: Number((element as HTMLElement).dataset.h3NlePixelsPerFrame),
        dpr: window.devicePixelRatio,
        outerScroll: window.scrollY,
      }));
      await page.keyboard.down("Control");
      await page.mouse.wheel(0, -120);
      await page.keyboard.up("Control");
      await expect
        .poll(async () =>
          Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame")),
        )
        .toBeGreaterThan(beforeZoom.scale);
      expect(await page.evaluate(() => window.devicePixelRatio)).toBe(
        beforeZoom.dpr,
      );
      expect(await page.evaluate(() => window.scrollY)).toBe(
        beforeZoom.outerScroll,
      );

      const beforePan = Number(
        await timeline.getAttribute("data-h3-nle-view-start"),
      );
      await page.keyboard.down("Shift");
      await page.mouse.wheel(0, 120);
      await page.keyboard.up("Shift");
      await expect
        .poll(async () =>
          Number(await timeline.getAttribute("data-h3-nle-view-start")),
        )
        .toBeGreaterThan(beforePan);

      await overlay.evaluate((overlayRoot) => {
        const runtime = window as any;
        runtime.__h3M25_59OrdinaryWheel = null;
        window.addEventListener(
          "wheel",
          (event) => {
            runtime.__h3M25_59OrdinaryWheel = {
              defaultPrevented: event.defaultPrevented,
              ctrlKey: event.ctrlKey,
              shiftKey: event.shiftKey,
            };
          },
          { once: true },
        );
        const probe = document.createElement("div");
        probe.id = "h3-content-extent-outside-scroll-probe";
        Object.assign(probe.style, {
          position: "fixed",
          right: "12px",
          bottom: "12px",
          width: "80px",
          height: "48px",
          overflow: "auto",
          overscrollBehavior: "contain",
          pointerEvents: "auto",
          background: "rgb(32, 33, 36)",
          zIndex: "2147483647",
        });
        const content = document.createElement("div");
        content.style.height = "400px";
        probe.append(content);
        // IMPORTANT: keep the ordinary-wheel probe in the modal top layer but
        // outside the timeline region; body children cannot receive pointer
        // input while the NLE dialog owns the browser top layer.
        overlayRoot.append(probe);
      });
      const outsideProbe = page.locator(
        "#h3-content-extent-outside-scroll-probe",
      );
      await outsideProbe.hover();
      await page.mouse.wheel(0, 240);
      await expect
        .poll(() => outsideProbe.evaluate((element) => element.scrollTop))
        .toBeGreaterThan(0);
      const ordinaryWheel = await page.evaluate<{
        defaultPrevented: boolean;
        ctrlKey: boolean;
        shiftKey: boolean;
      }>(() => (window as any).__h3M25_59OrdinaryWheel);
      expect(ordinaryWheel).toEqual({
        defaultPrevented: false,
        ctrlKey: false,
        shiftKey: false,
      });
      await outsideProbe.evaluate((element) => element.remove());

      await overlay.locator('[data-h3-nle-action="close"]').click();
      await expect(overlay).toHaveCount(0);
      await shell.locator('[data-h3-nle-entry="open"]').click();
      await expect(overlay).toBeVisible();
      const reboundTimeline = overlay.locator(
        '[data-h3-nle-region="timeline"]',
      );
      const reboundGrid = overlay.getByRole("grid");
      await overlay
        .locator('[data-h3-nle-control="transport.zoom_fit"]')
        .click();
      const reboundScale = Number(
        await reboundTimeline.getAttribute("data-h3-nle-pixels-per-frame"),
      );
      const reboundGridBox = await reboundGrid.boundingBox();
      if (reboundGridBox === null)
        throw new Error("reopened supplied-host timeline is unavailable");
      await page.mouse.move(
        reboundGridBox.x + reboundGridBox.width * 0.65,
        reboundGridBox.y + reboundGridBox.height * 0.5,
      );
      await page.keyboard.down("Control");
      await page.mouse.wheel(0, -120);
      await page.keyboard.up("Control");
      await expect
        .poll(
          async () =>
            Number(
              await reboundTimeline.getAttribute(
                "data-h3-nle-pixels-per-frame",
              ),
            ) / reboundScale,
        )
        .toBeCloseTo(Math.SQRT2, 3);
      contentNavigationEvidence = {
        trailingPadding,
        beforeZoomScale: beforeZoom.scale,
        pageDprStable: true,
        outerScrollStable: true,
        shiftPanAdvanced: true,
        outsideWheelDefaultPrevented: ordinaryWheel.defaultPrevented,
        outsideScrollEffective: true,
        reopenSingleWheelFactor: Math.SQRT2,
      };
    }

    const afterSecondDelete = await remove(second, 1);
    await expectExtent(overlay, afterSecondDelete, SOURCE_FRAMES);
    const restoredSecond = await undo();
    await expectExtent(overlay, restoredSecond, SOURCE_FRAMES * 2);
    const removedSecondAgain = await redo();
    await expectExtent(overlay, removedSecondAgain, SOURCE_FRAMES);

    const empty = await remove(removedSecondAgain, 0);
    await expectExtent(overlay, empty, 0);
    const restoredLast = await undo();
    await expectExtent(overlay, restoredLast, SOURCE_FRAMES);
    const emptyAgain = await redo();
    await expectExtent(overlay, emptyAgain, 0);
    const readded = await add(videoAssets[1]!.assetId);
    await expectExtent(overlay, readded, SOURCE_FRAMES);
    expect(readded.authoring.clips[0]).toMatchObject({
      startFrame: 0,
      durationFrames: SOURCE_FRAMES,
      sourceStartFrame: 0,
    });
    expect(transactionOrdinal).toBe(11);

    await overlay.locator('[data-h3-nle-action="close"]').click();
    await expect(overlay).toHaveCount(0);
    const released = encodeAuthoringAction(
      `content-extent-release-${authoringHandle.slice(-16)}`,
      "release_workspace",
      { workspace_handle: authoringHandle },
    );
    const releaseStatus = await page.evaluate(async (payload) => {
      const response = await fetch("/h3-context/v1/authoring/action", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      return response.status;
    }, released);
    expect(releaseStatus).toBe(204);
    authoringHandle = undefined;

    await page.evaluate((containerId) => {
      const runtime = window as any;
      const app = runtime.comfyAPI.app.app;
      if (runtime.__h3ContentExtentAnchor)
        app.graph.remove(runtime.__h3ContentExtentAnchor);
      if (runtime.__h3ContentExtentNative)
        app.graph.remove(runtime.__h3ContentExtentNative);
      document.getElementById(containerId)?.remove();
    }, CONTAINER_ID);
    const hostAfter = await page.evaluate(() => {
      const runtime = window as any;
      const app = runtime.comfyAPI.app.app;
      const baseline = runtime.__h3ContentExtentBaseline;
      return {
        graph: structuredClone(app.graph.serialize()),
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
    });
    const surroundings = diffGraphSurroundings({
      beforeValue: graphBefore,
      afterValue: hostAfter.graph,
      reference: {
        ownedNodeIds: [],
        ownedLinkIds: [],
        anchorNodeId: "",
        ownedProjectionEqual: true,
      },
    });
    expect(hostAfter.workflowIdentityStable).toBe(true);
    expect(hostAfter.openWorkflowCountStable).toBe(true);
    expect(hostAfter.ownedMountCountStable).toBe(true);
    expect(surroundings.counts.owned).toBe(0);
    expect(context.pages()).toHaveLength(pageCount);
    expect(await supportedHostQueueCounts(page)).toEqual(queueBefore);
    expect(promptRequests).toBe(0);
    const networkEvidence = network.snapshot();
    expect(networkEvidence.interactionRemoteCount).toBe(0);
    expect(networkEvidence.interactionProviderCount).toBe(0);
    expectCandidateInteractionNetworkLocal(candidateNetwork);
    const evidence = {
      schema: "h3.context.content_extent_host.v1",
      candidate: {
        bundleSha256: candidateBundle.sha256,
        installedInventorySha256,
        backendInventorySha256: candidateBackendRuntime.inventorySha256,
      },
      initial: { contentEndExclusive: 0, renderSnapshot: null },
      acceptedExtents: receipts.map((receipt) => ({
        commands: receipt.commands.map((command) => command.kind),
        contentEndExclusive: receipt.authoring.contentEndExclusive,
        renderDurationFrames:
          receipt.renderSnapshot?.output.durationFrames ?? null,
      })),
      transactionCount: transactionOrdinal,
      promptRequests,
      queueStable: true,
      hostStateStable: true,
      surroundings,
      network: networkEvidence,
      contentNavigation: contentNavigationEvidence,
    };
    await testInfo.attach("content-extent-host-qualification", {
      contentType: "application/json",
      body: JSON.stringify(evidence),
    });
    console.log(JSON.stringify({ contentExtentHostQualification: evidence }));
  } finally {
    if (await overlay.count())
      await overlay.locator('[data-h3-nle-action="close"]').click();
    if (authoringHandle) {
      const payload = encodeAuthoringAction(
        `content-extent-cleanup-${authoringHandle.slice(-16)}`,
        "release_workspace",
        { workspace_handle: authoringHandle },
      );
      const status = await page.evaluate(async (body) => {
        const response = await fetch("/h3-context/v1/authoring/action", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        return response.status;
      }, payload);
      expect.soft(status).toBe(204);
    }
    await page.evaluate((containerId) => {
      const runtime = window as any;
      const app = runtime.comfyAPI?.app?.app;
      if (!app) return;
      if (runtime.__h3ContentExtentAnchor)
        app.graph.remove(runtime.__h3ContentExtentAnchor);
      if (runtime.__h3ContentExtentNative)
        app.graph.remove(runtime.__h3ContentExtentNative);
      document.getElementById(containerId)?.remove();
      document
        .getElementById("h3-content-extent-outside-scroll-probe")
        ?.remove();
    }, CONTAINER_ID);
  }
});
