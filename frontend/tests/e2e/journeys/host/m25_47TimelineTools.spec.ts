import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import {
  decodeTimelineReceipt,
  encodeAuthoringAction,
  type TimelineReceipt,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import { seekPlayhead } from "../../helpers/nleTimeline";
import {
  assertCandidateBundleInjection,
  candidateBundle,
  candidateInjectionCount,
  hostUrl,
  repositoryRoot,
} from "../../host/candidate";
import { expect, test } from "../../host/fixture";

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

type WorkspaceFixture = Readonly<{
  schema: string;
  output: unknown;
  anchorId: number;
  promptId: string;
  qualification: Readonly<{
    schema: string;
    candidateBundleSha256: string;
  }>;
}>;

async function loadFixture(): Promise<WorkspaceFixture> {
  const configured = process.env.H3_CONTEXT_NLE_WORKSPACE_FIXTURE;
  if (!configured) throw new Error("owned fixture required");
  const fixture = JSON.parse(
    await readFile(resolve(repositoryRoot, configured), "utf8"),
  ) as WorkspaceFixture;
  if (fixture.schema !== "NleWorkspaceHostFixtureV1")
    throw new Error("fixture contract mismatch");
  expect(fixture.qualification).toMatchObject({
    schema: "M25RealRuntimeQualificationV1",
    candidateBundleSha256: candidateBundle?.sha256,
  });
  return fixture;
}

test("timeline tools contain shortcuts and preserve the owned supplied-host projection", async ({
  page,
  context,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_M25_47_HOST !== "1",
    "explicit model-free M25-47 supplied-host sample required",
  );
  test.setTimeout(240_000);
  if (!hostUrl) throw new Error("supplied host required");

  const fixture = await loadFixture();
  const receipts: TimelineReceipt[] = [];
  const authoringExchanges: Array<{
    action: string;
    status: number;
    code: string | null;
  }> = [];
  const leaseExchanges: Array<{
    operation: string;
    status: number;
    reason: string | null;
  }> = [];
  let authoringHandle: string | undefined;
  let queueCalls = 0;
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname.replace(/^\/api(?=\/)/, "");
    if (path === "/prompt" && request.method() === "POST") queueCalls += 1;
  });
  page.on("response", async (response) => {
    const path = new URL(response.url()).pathname.replace(
      /^\/api(?=\/h3-context\/)/,
      "",
    );
    if (path.startsWith("/h3-context/v1/authoring/media-source-leases")) {
      const operation = response.request().postDataJSON()?.operation;
      if (
        typeof operation !== "string" ||
        !/^[a-z0-9_]{1,100}$/.test(operation)
      )
        return;
      const status = response.status();
      const exchange = { operation, status, reason: null as string | null };
      leaseExchanges.push(exchange);
      if (status >= 400) {
        try {
          const body = (await response.json()) as { reason?: unknown };
          if (
            typeof body.reason === "string" &&
            /^[a-z0-9_]{1,100}$/.test(body.reason)
          )
            exchange.reason = body.reason;
        } catch {
          exchange.reason = "body_unavailable";
        }
      }
      return;
    }
    if (path !== "/h3-context/v1/authoring/action") return;
    const action = response.request().postDataJSON()?.action;
    if (typeof action !== "string" || !/^[a-z0-9_]{1,100}$/.test(action))
      return;
    const status = response.status();
    const exchange = { action, status, code: null as string | null };
    authoringExchanges.push(exchange);
    let body: unknown = null;
    if (
      status !== 204 &&
      (status >= 400 ||
        action === "create_authoring_workspace" ||
        action === "apply_timeline_transaction")
    ) {
      try {
        body = await response.json();
      } catch {
        exchange.code = "body_unavailable";
      }
    }
    if (
      body !== null &&
      typeof body === "object" &&
      !Array.isArray(body) &&
      "error" in body &&
      body.error !== null &&
      typeof body.error === "object" &&
      !Array.isArray(body.error) &&
      "code" in body.error &&
      typeof body.error.code === "string" &&
      /^[a-z0-9_]{1,100}$/.test(body.error.code)
    )
      exchange.code = body.error.code;
    if (action === "create_authoring_workspace" && status === 201) {
      if (
        body !== null &&
        typeof body === "object" &&
        !Array.isArray(body) &&
        "workspace_handle" in body &&
        typeof body.workspace_handle === "string"
      )
        authoringHandle = body.workspace_handle;
    }
    if (action === "apply_timeline_transaction" && status === 200)
      receipts.push(decodeTimelineReceipt(body));
  });
  const waitReceipt = async (count: number) => {
    await expect.poll(() => receipts.length).toBe(count);
    return receipts.at(-1)!;
  };

  const injectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() =>
    (window as any).comfyAPI?.app?.app?.extensionManager
      ?.getSidebarTabs?.()
      .some((tab: any) => tab.id === "h3-context"),
  );
  await assertCandidateBundleInjection(page, context, injectionCount);
  await page.waitForFunction(
    () =>
      (window as any).comfyAPI.app.app.extensionManager.workflow
        ?.activeWorkflow != null,
  );
  await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((entry: any) => entry.id === "h3-context");
    const panel = document.createElement("div");
    panel.id = "h3-m25-47-host-container";
    panel.className = "side-bar-panel sidebar-content-container";
    Object.assign(panel.style, {
      position: "fixed",
      inset: "80px auto 0 60px",
      width: "688px",
      overflow: "auto",
      zIndex: "2000",
      background: "#202124",
    });
    document.body.append(panel);
    tab.render(panel);
    runtime.__m2547HostIdentity = {
      workflow: app.extensionManager.workflow.activeWorkflow,
      tabs: app.extensionManager.workflow.openWorkflows.length,
    };
    runtime.__m2547HostKeys = [];
    window.addEventListener("keydown", (event) => {
      runtime.__m2547HostKeys.push({
        key: event.key,
        ctrl: event.ctrlKey,
        shift: event.shiftKey,
      });
    });
  });
  await page.evaluate(({ output, anchorId, promptId }) => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
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
    runtime.__m2547Anchor = anchor;
    runtime.__m2547Native = native;
    runtime.comfyAPI.api.api.dispatchEvent(
      new CustomEvent("executed", {
        detail: { node: anchorId, prompt_id: promptId, output },
      }),
    );
  }, fixture);
  const graphBefore = await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const projection = () =>
      [runtime.__m2547Anchor, runtime.__m2547Native].map((node: any) => ({
        id: node.id,
        type: node.type,
        inputs: (node.inputs ?? []).map((input: any) => ({
          name: input.name,
          link: input.link ?? null,
        })),
        outputs: (node.outputs ?? []).map((output: any) => ({
          name: output.name,
          links: output.links ?? [],
        })),
        prompt:
          node.widgets?.find((widget: any) => widget.name === "prompt")
            ?.value ?? null,
      }));
    runtime.__m2547Projection = projection;
    runtime.__m2547ProjectionBefore = JSON.stringify(projection());
    return app.graph.serialize();
  });

  const shell = page.locator("#h3-m25-47-host-container");
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  try {
    await shell.locator('[data-page-id="production"]').click();
    const clipEditor = shell.locator(
      '[data-h3-director-function="clip_editor"]',
    );
    await clipEditor.click();
    const start = shell.getByRole("button", {
      name: "Start authoring from this context",
      exact: true,
    });
    await start.click();
    await expect(start).toHaveCount(0);

    // The listener is genuinely observable outside the owned overlay. The same key below must
    // disappear at the overlay boundary without wrapping or disabling this foreign callback.
    await clipEditor.focus();
    await page.keyboard.press("q");
    await expect
      .poll(() => page.evaluate(() => (window as any).__m2547HostKeys.length))
      .toBe(1);
    await page.evaluate(() => ((window as any).__m2547HostKeys = []));

    await shell.locator('[data-h3-nle-entry="open"]').click();
    const timelineStatus = overlay.locator('[data-h3-nle-status="timeline"]');
    await expect
      .poll(async () => {
        const state = await timelineStatus.getAttribute(
          "data-h3-nle-authoring",
        );
        return state === "error"
          ? `error:${JSON.stringify(authoringExchanges)}`
          : state;
      })
      .toBe("ready");
    // IMPORTANT: the HTTP receipt precedes the bounded history refresh. Wait for that exact
    // transaction to return to ready before issuing another CAS or the host correctly returns 409.
    const waitAcceptedReceipt = async (count: number) => {
      const receipt = await waitReceipt(count);
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
    let acceptedReceiptCount = 0;
    const nextAcceptedReceipt = () =>
      waitAcceptedReceipt(++acceptedReceiptCount);

    // IMPORTANT: keep the real qualification fixture's backend asset identity; `image_1`
    // never enters this workspace and makes selectOption wait for a nonexistent option.
    const imageAssetId = "first_frame_1";
    // IMPORTANT: M25-48 replaced the generic insert form with source-bound Media cards. Keep this
    // host row on that accepted surface; restoring the form would test an obsolete parallel UI.
    await overlay.locator('[data-h3-nle-pane="assets"]').click();
    // IMPORTANT: Media decoration and clip playback share one serial backend build owner. A
    // rendered canvas is not readiness; wait for both thumbnails to publish, then keep Media
    // mounted across insertion so this row proves playback preempts subsequent decoration demand.
    await expect(
      overlay.locator("[data-h3-nle-asset] .h3-nle-thumbnail-status"),
    ).toHaveText(["", ""]);
    const mediaPane = overlay.locator('[data-h3-nle-pane="assets"]');
    await overlay
      .locator(
        '[data-h3-nle-asset="video_1"] [data-h3-nle-control="asset.insert"]',
      )
      .click();
    const videoReceipt = await nextAcceptedReceipt();
    await expect(mediaPane).toHaveAttribute("aria-selected", "true");
    const video = videoReceipt.snapshot.clips.find(
      (clip) => clip.assetId === "video_1",
    )!;
    const videoDuration = video.durationFrames;
    const ruler = overlay.getByRole("slider", {
      name: "Playhead",
      exact: true,
    });
    try {
      await expect
        .poll(async () => ({
          ruler: await ruler.getAttribute("aria-disabled"),
          monitor: await overlay
            .locator('[data-h3-nle-status="monitor"]')
            .textContent(),
        }))
        .toEqual({ ruler: "false", monitor: "Monitor paused." });
    } catch {
      throw new Error(
        `monitor readiness failed: ${JSON.stringify({
          ruler: await ruler.getAttribute("aria-disabled"),
          monitor: await overlay
            .locator('[data-h3-nle-status="monitor"]')
            .textContent(),
          leaseExchanges,
        })}`,
      );
    }
    await seekPlayhead(page, ruler, 60);
    const trackEditor = overlay.getByRole("region", {
      name: "Track",
      exact: true,
    });
    await trackEditor
      .getByLabel("Kind", { exact: true })
      .selectOption("image_overlay");
    await trackEditor.locator('[data-h3-nle-control="track.add"]').click();
    const trackReceipt = await nextAcceptedReceipt();
    const imageTrackId = trackReceipt.snapshot.tracks.find(
      (track) => track.kind === "image_overlay",
    )?.trackId;
    if (imageTrackId === undefined)
      throw new Error("qualification image track unavailable");

    await overlay.locator('[data-h3-nle-pane="assets"]').click();
    await overlay
      .locator(
        `[data-h3-nle-asset="${imageAssetId}"] [data-h3-nle-control="asset.insert"]`,
      )
      .click();
    const imageReceipt = await nextAcceptedReceipt();
    await expect(mediaPane).toHaveAttribute("aria-selected", "true");
    const image = imageReceipt.snapshot.clips.find(
      (clip) => clip.assetId === imageAssetId,
    )!;

    const selectClip = async (clipId: string) => {
      const control = overlay.locator(
        `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
      );
      if ((await control.getAttribute("aria-pressed")) === "true") return;
      // IMPORTANT: selection belongs to this button's accessible command, not its transient
      // virtual-timeline hit box. Space selects; Enter intentionally starts a keyboard move draft.
      await control.focus();
      await page.keyboard.press("Space");
      await nextAcceptedReceipt();
    };
    await selectClip(video.clipId);
    await expect(ruler).toHaveAttribute("aria-disabled", "false");
    const splitOffset = Math.min(12, videoDuration - 1);
    await seekPlayhead(page, ruler, video.startFrame + splitOffset);
    await overlay.locator('[data-h3-nle-control="clip.split"]').click();
    const split = await nextAcceptedReceipt();
    const splitVideo = split.snapshot.clips
      .filter((clip) => clip.assetId === "video_1")
      .sort((left, right) => left.startFrame - right.startFrame);
    expect(splitVideo).toMatchObject([
      {
        clipId: video.clipId,
        startFrame: video.startFrame,
        durationFrames: splitOffset,
      },
      {
        startFrame: video.startFrame + splitOffset,
        durationFrames: videoDuration - splitOffset,
      },
    ]);
    await overlay.locator('[data-h3-nle-control="history.undo"]').click();
    const unsplit = await nextAcceptedReceipt();
    expect(
      unsplit.snapshot.clips.filter((clip) => clip.assetId === "video_1"),
    ).toMatchObject([
      {
        clipId: video.clipId,
        startFrame: video.startFrame,
        durationFrames: videoDuration,
      },
    ]);

    await selectClip(video.clipId);
    await expect(ruler).toHaveAttribute("aria-disabled", "false");
    const trimOffset = Math.min(36, videoDuration - 1);
    await seekPlayhead(page, ruler, video.startFrame + trimOffset);
    const grid = overlay.getByRole("grid", {
      name: "Timeline tracks",
      exact: true,
    });
    await grid.focus();
    await page.keyboard.press("w");
    const trimmed = await nextAcceptedReceipt();
    expect(
      trimmed.snapshot.clips.find((clip) => clip.clipId === video.clipId),
    ).toMatchObject({
      startFrame: video.startFrame,
      durationFrames: trimOffset,
    });
    expect(await page.evaluate(() => (window as any).__m2547HostKeys)).toEqual(
      [],
    );
    await overlay.locator('[data-h3-nle-control="history.undo"]').click();
    await nextAcceptedReceipt();

    await overlay.locator('[data-h3-nle-control="transport.ripple"]').click();
    await overlay
      .locator('[data-h3-nle-control="range.ripple_delete"]')
      .click();
    const deleted = await nextAcceptedReceipt();
    expect(
      deleted.snapshot.clips.some((clip) => clip.clipId === video.clipId),
    ).toBe(false);
    await overlay.locator('[data-h3-nle-control="history.undo"]').click();
    const restored = await nextAcceptedReceipt();
    expect(
      restored.snapshot.clips.some((clip) => clip.clipId === video.clipId),
    ).toBe(true);
    await overlay.locator('[data-h3-nle-control="transport.ripple"]').click();

    await selectClip(video.clipId);
    await expect(ruler).toHaveAttribute("aria-disabled", "false");
    await seekPlayhead(page, ruler, video.startFrame + splitOffset);
    await overlay.locator('[data-h3-nle-control="clip.split"]').click();
    const resplit = await nextAcceptedReceipt();
    const right = resplit.snapshot.clips.find(
      (clip) => clip.assetId === "video_1" && clip.clipId !== video.clipId,
    )!;
    const cut = overlay.locator(
      `[data-h3-nle-control="boundary.roll"][data-h3-nle-roll-left="${video.clipId}"][data-h3-nle-roll-right="${right.clipId}"]`,
    );
    await cut.click();
    await overlay
      .getByRole("button", { name: "Roll edit +1", exact: true })
      .click();
    await overlay.getByRole("button", { name: "Apply", exact: true }).click();
    const rolled = await nextAcceptedReceipt();
    expect(
      rolled.snapshot.clips.find((clip) => clip.clipId === video.clipId),
    ).toMatchObject({
      startFrame: video.startFrame,
      durationFrames: splitOffset + 1,
    });
    expect(
      rolled.snapshot.clips.find((clip) => clip.clipId === right.clipId),
    ).toMatchObject({
      startFrame: video.startFrame + splitOffset + 1,
      durationFrames: videoDuration - splitOffset - 1,
    });

    const timeline = overlay.locator('[data-h3-nle-region="timeline"]');
    const scaleBefore = Number(
      await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
    );
    await grid.focus();
    await page.keyboard.press("Control+-");
    await expect
      .poll(async () =>
        Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame")),
      )
      .toBeLessThan(scaleBefore);
    // A standalone modifier is not a host shortcut activation. The owned non-modifier key must
    // stay contained, then the next shortcut gets a fresh independently observable sample.
    expect(
      await page.evaluate(() =>
        (window as any).__m2547HostKeys.filter(
          (entry: { key: string }) => entry.key !== "Control",
        ),
      ),
    ).toEqual([]);
    await page.evaluate(() => ((window as any).__m2547HostKeys = []));
    await page.keyboard.press("Space");
    expect(await page.evaluate(() => (window as any).__m2547HostKeys)).toEqual(
      [],
    );

    await overlay.locator('[data-h3-nle-control="toolbar.more"]').click();
    await overlay
      .locator('[data-h3-nle-alternative="selection.clear"]')
      .click();
    await nextAcceptedReceipt();
    const splitControl = overlay.locator('[data-h3-nle-control="clip.split"]');
    await expect(splitControl).toHaveAttribute("aria-disabled", "true");
    await grid.focus();
    await page.keyboard.press("Control+b");
    expect(receipts).toHaveLength(acceptedReceiptCount);
    expect(
      await page.evaluate(() =>
        (window as any).__m2547HostKeys.filter(
          (entry: { key: string }) => entry.key !== "Control",
        ),
      ),
    ).toEqual([]);

    await selectClip(image.clipId);
    const editable = overlay
      .getByRole("region", { name: "Clip", exact: true })
      .getByLabel("Source start frame", { exact: true });
    await editable.focus();
    await page.keyboard.press("q");
    await expect(editable).toBeFocused();
    expect(receipts).toHaveLength(acceptedReceiptCount);

    const after = await page.evaluate(() => {
      const runtime = window as any;
      const app = runtime.comfyAPI.app.app;
      return {
        graph: app.graph.serialize(),
        ownedIds: [runtime.__m2547Anchor.id, runtime.__m2547Native.id].map(
          String,
        ),
        projectionStable:
          JSON.stringify(runtime.__m2547Projection()) ===
          runtime.__m2547ProjectionBefore,
        workflowStable:
          app.extensionManager.workflow.activeWorkflow ===
          runtime.__m2547HostIdentity.workflow,
        tabsStable:
          app.extensionManager.workflow.openWorkflows.length ===
          runtime.__m2547HostIdentity.tabs,
      };
    });
    expect(after.projectionStable).toBe(true);
    expect(after.workflowStable).toBe(true);
    expect(after.tabsStable).toBe(true);
    expect(context.pages()).toHaveLength(1);
    expect(queueCalls).toBe(0);

    const surroundings = diffGraphSurroundings({
      beforeValue: graphBefore,
      afterValue: after.graph,
      reference: {
        ownedNodeIds: after.ownedIds,
        ownedLinkIds: [],
        anchorNodeId: String(fixture.anchorId),
        ownedProjectionEqual: after.projectionStable,
      },
    });
    await testInfo.attach("h3_m25_47_timeline_tools", {
      body: Buffer.from(
        JSON.stringify({
          receiptCount: receipts.length,
          finalRevision: receipts.at(-1)?.snapshot.timelineRevision,
          queueCalls,
          hostListenerObservedOutside: true,
          scopedKeysContained: true,
        }),
        "utf8",
      ),
      contentType: "application/json",
    });
    await testInfo.attach("h3_host_surroundings_diff", {
      body: Buffer.from(JSON.stringify(surroundings), "utf8"),
      contentType: "application/json",
    });
  } finally {
    if (await overlay.count())
      await overlay.locator('[data-h3-nle-action="close"]').click();
    if (authoringHandle) {
      const body = encodeAuthoringAction(
        `m25-47-host-release-${authoringHandle.slice(-16)}`,
        "release_workspace",
        { workspace_handle: authoringHandle },
      );
      const status = await page.evaluate(
        async (payload) =>
          (
            await fetch("/h3-context/v1/authoring/action", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(payload),
            })
          ).status,
        body,
      );
      expect.soft(status).toBe(204);
    }
    await page.evaluate(() => {
      const runtime = window as any;
      const app = runtime.comfyAPI.app.app;
      if (runtime.__m2547Anchor) app.graph.remove(runtime.__m2547Anchor);
      if (runtime.__m2547Native) app.graph.remove(runtime.__m2547Native);
      document.getElementById("h3-m25-47-host-container")?.remove();
    });
  }
});
