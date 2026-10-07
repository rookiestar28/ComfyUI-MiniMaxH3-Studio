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

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
});

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

test("direct timeline manipulation stays owned and queue-free on the supplied host", async ({
  page,
  context,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_M25_46_HOST !== "1",
    "explicit model-free supplied-host sample required",
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
  let authoringHandle: string | undefined;
  let queueCalls = 0;
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname.replace(/^\/api(?=\/)/, "");
    if (path === "/prompt" && request.method() === "POST") queueCalls += 1;
  });
  page.on("response", async (response) => {
    if (
      new URL(response.url()).pathname.replace(
        /^\/api(?=\/h3-context\/)/,
        "",
      ) !== "/h3-context/v1/authoring/action"
    )
      return;
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
    if (action === "create_authoring_workspace" && response.status() === 201) {
      if (
        body !== null &&
        typeof body === "object" &&
        !Array.isArray(body) &&
        "workspace_handle" in body &&
        typeof body.workspace_handle === "string"
      )
        authoringHandle = body.workspace_handle;
    }
    if (action === "apply_timeline_transaction" && response.status() === 200)
      receipts.push(decodeTimelineReceipt(body));
  });

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
    panel.id = "h3-m25-46-host-container";
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
    runtime.__m2546HostIdentity = {
      workflow: app.extensionManager.workflow.activeWorkflow,
      tabs: app.extensionManager.workflow.openWorkflows.length,
    };
    runtime.__m2546HostKeys = [];
    document.addEventListener("keydown", (event) => {
      runtime.__m2546HostKeys.push(event.key);
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
    runtime.__m2546Anchor = anchor;
    runtime.__m2546Native = native;
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
      [runtime.__m2546Anchor, runtime.__m2546Native].map((node: any) => ({
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
    runtime.__m2546Projection = projection;
    runtime.__m2546ProjectionBefore = JSON.stringify(projection());
    return app.graph.serialize();
  });

  const shell = page.locator("#h3-m25-46-host-container");
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  try {
    await shell.locator('[data-page-id="production"]').click();
    await shell.locator('[data-h3-director-function="clip_editor"]').click();
    const start = shell.getByRole("button", {
      name: "Start authoring from this context",
      exact: true,
    });
    await start.click();
    // IMPORTANT: opening while create is still pending skips history initialization and leaves
    // the timeline in error. Wait for the accepted projection, not merely the click dispatch.
    await expect(start).toHaveCount(0);
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

    await overlay.locator('[data-h3-nle-pane="assets"]').click();
    const videoCard = overlay.locator('[data-h3-nle-card-index="2"]');
    await expect(videoCard).toBeVisible();
    await videoCard.locator('[data-h3-nle-control="asset.insert"]').click();
    await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);
    await expect.poll(() => receipts.length).toBe(1);

    const ruler = overlay.getByRole("slider", {
      name: "Playhead",
      exact: true,
    });
    const picture = overlay.locator('[data-h3-nle-canvas="composition"]');
    // IMPORTANT: authoring readiness precedes the accepted monitor transport publication on a
    // real host. Sending the key while the ruler is still disabled drops the interaction.
    await expect(ruler).toHaveAttribute("aria-disabled", "false");
    await ruler.focus();
    await page.keyboard.press("ArrowRight");
    await expect(ruler).toHaveAttribute("aria-valuenow", "1");
    await expect(picture).toHaveAttribute("data-h3-nle-presented-frame", "1");
    expect(await page.evaluate(() => (window as any).__m2546HostKeys)).toEqual(
      [],
    );
    await seekPlayhead(page, ruler, 12);
    await expect(picture).toHaveAttribute("data-h3-nle-presented-frame", "12");
    const presentedAtRulerRequest = Number(
      await picture.getAttribute("data-h3-nle-presented-frame"),
    );

    const clip = overlay.locator(
      '[data-h3-nle-clip] [data-h3-nle-control="selection.set"]',
    );
    const beforeMove = receipts.at(-1)!.snapshot.clips[0]!;
    const clipBox = await clip.boundingBox();
    if (!clipBox) throw new Error("clip_not_visible");
    await page.mouse.move(
      clipBox.x + clipBox.width / 2,
      clipBox.y + clipBox.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(
      clipBox.x + clipBox.width / 2 + 18,
      clipBox.y + clipBox.height / 2,
      { steps: 5 },
    );
    await page.mouse.up();
    await expect.poll(() => receipts.length).toBe(2);
    const moved = receipts.at(-1)!.snapshot.clips[0]!;
    expect(moved.startFrame).toBeGreaterThan(beforeMove.startFrame);

    const trimCount = receipts.length;
    const grip = overlay
      .locator('[data-h3-nle-trim-edge="end"]:not([hidden])')
      .filter({ visible: true })
      .first();
    const gripBox = await grip.boundingBox();
    if (!gripBox) throw new Error("trim_grip_not_visible");
    await page.mouse.move(
      gripBox.x + gripBox.width / 2,
      gripBox.y + gripBox.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(
      gripBox.x + gripBox.width / 2 - 12,
      gripBox.y + gripBox.height / 2,
      { steps: 4 },
    );
    await page.mouse.up();
    await expect.poll(() => receipts.length).toBe(trimCount + 1);
    expect(receipts.at(-1)!.snapshot.clips[0]!.durationFrames).toBeLessThan(
      moved.durationFrames,
    );
    await overlay.locator('[data-h3-nle-control="history.undo"]').click();
    await expect.poll(() => receipts.length).toBe(trimCount + 2);
    expect(receipts.at(-1)!.snapshot.clips[0]).toEqual(moved);

    const timeline = overlay.locator('[data-h3-nle-region="timeline"]');
    const scaleBefore = Number(
      await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
    );
    await overlay.locator('[data-h3-nle-control="transport.zoom_in"]').click();
    await expect
      .poll(async () =>
        Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame")),
      )
      .toBeGreaterThan(scaleBefore);
    const viewBefore = Number(
      await timeline.getAttribute("data-h3-nle-view-start"),
    );
    await overlay.locator('[data-h3-nle-alternative="pan.later"]').click();
    await expect
      .poll(async () =>
        Number(await timeline.getAttribute("data-h3-nle-view-start")),
      )
      .toBeGreaterThan(viewBefore);

    const after = await page.evaluate(() => {
      const runtime = window as any;
      const app = runtime.comfyAPI.app.app;
      return {
        graph: app.graph.serialize(),
        ownedIds: [runtime.__m2546Anchor.id, runtime.__m2546Native.id].map(
          String,
        ),
        projectionStable:
          JSON.stringify(runtime.__m2546Projection()) ===
          runtime.__m2546ProjectionBefore,
        workflowStable:
          app.extensionManager.workflow.activeWorkflow ===
          runtime.__m2546HostIdentity.workflow,
        tabsStable:
          app.extensionManager.workflow.openWorkflows.length ===
          runtime.__m2546HostIdentity.tabs,
        presentedFrame: Number(
          document
            .querySelector('[data-h3-nle-canvas="composition"]')
            ?.getAttribute("data-h3-nle-presented-frame"),
        ),
      };
    });
    expect(after.projectionStable).toBe(true);
    expect(after.workflowStable).toBe(true);
    expect(after.tabsStable).toBe(true);
    expect(context.pages()).toHaveLength(1);
    expect(queueCalls).toBe(0);
    expect(presentedAtRulerRequest).toBe(12);
    await testInfo.attach("h3_m25_46_transport_observation", {
      body: Buffer.from(
        JSON.stringify({
          requestedFrame: 12,
          presentedAtRulerRequest,
          presentedAfterTimelineTransactions: after.presentedFrame,
        }),
        "utf8",
      ),
      contentType: "application/json",
    });

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
    await testInfo.attach("h3_host_surroundings_diff", {
      body: Buffer.from(JSON.stringify(surroundings), "utf8"),
      contentType: "application/json",
    });
  } finally {
    if (await overlay.count())
      await overlay.locator('[data-h3-nle-action="close"]').click();
    if (authoringHandle) {
      const body = encodeAuthoringAction(
        `m25-46-host-release-${authoringHandle.slice(-16)}`,
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
      if (runtime.__m2546Anchor) app.graph.remove(runtime.__m2546Anchor);
      if (runtime.__m2546Native) app.graph.remove(runtime.__m2546Native);
      document.getElementById("h3-m25-46-host-container")?.remove();
    });
  }
});
