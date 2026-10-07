import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import type { BrowserContext, Locator, Page } from "@playwright/test";
import {
  encodeAuthoringAction,
  decodeTimelineReceipt,
  type TimelineReceipt,
} from "../../../../src/contracts/authoringWorkbenchCodec";
import { startProcessAudioObserver } from "../../host/audioObserver";
import { openExportPanel } from "../../helpers/nleExport";
import { loseCanvasContext } from "../../helpers/nleMonitorPicture";
import {
  expectNarrowReferenceShell,
  expectStandardReferenceShell,
  summarizeReferenceShell,
} from "../../helpers/nleReferenceShellHost";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import { test, expect } from "../../host/fixture";
import {
  assertCandidateBundleInjection,
  candidateInjectionCount,
  hostUrl,
  repositoryRoot,
  candidateBundle,
} from "../../host/candidate";

// The fixture publishes a genuine, queue-free Context with a bound synthetic VIDEO.
// Only its executed notification is delivered here; workspace, history, media leases
// and edits all use the shipped shell and the supplied host's actual HTTP routes.
test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  launchOptions: { ignoreDefaultArgs: ["--mute-audio"] },
});

type M2555HostWorkspace = Readonly<{
  overlay: Locator;
  successfulTransactions: null[];
  cleanup(): Promise<void>;
}>;

async function openM2555HostWorkspace(
  page: Page,
  context: BrowserContext,
): Promise<M2555HostWorkspace> {
  if (!hostUrl) throw new Error("supplied host required");
  const configured = process.env.H3_CONTEXT_NLE_WORKSPACE_FIXTURE;
  if (!configured) throw new Error("owned fixture required");
  const fixture = JSON.parse(
    await readFile(resolve(repositoryRoot, configured), "utf8"),
  );
  if (fixture.schema !== "NleWorkspaceHostFixtureV1")
    throw new Error("fixture contract mismatch");
  expect(fixture.qualification).toMatchObject({
    schema: "M25RealRuntimeQualificationV1",
    normalStartupMediaActive: true,
    activeModulesInstalledOnly: true,
    backupModulesExcluded: true,
    canonicalNodesRegistered: true,
    candidateBundleSha256: candidateBundle?.sha256,
  });

  let authoringHandle: string | undefined;
  const successfulTransactions: null[] = [];
  const observeAuthoring = async (response: {
    url(): string;
    status(): number;
    request(): { postDataJSON(): unknown };
    json(): Promise<unknown>;
  }) => {
    const path = new URL(response.url()).pathname.replace(
      /^\/api(?=\/h3-context\/)/,
      "",
    );
    if (path !== "/h3-context/v1/authoring/action") return;
    const request = response.request().postDataJSON() as { action?: string };
    if (
      request.action === "apply_timeline_transaction" &&
      response.status() === 200
    )
      successfulTransactions.push(null);
    if (
      request.action === "create_authoring_workspace" &&
      response.status() === 201
    ) {
      const body = (await response.json()) as { workspace_handle?: unknown };
      if (typeof body.workspace_handle === "string")
        authoringHandle = body.workspace_handle;
    }
  };
  page.on("response", observeAuthoring);

  const injectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() =>
    (window as any).comfyAPI?.app?.app?.extensionManager
      ?.getSidebarTabs?.()
      .some((tab: any) => tab.id === "h3-context"),
  );
  await assertCandidateBundleInjection(page, context, injectionCount);
  await page.waitForFunction(() => {
    const app = (window as any).comfyAPI.app.app;
    return app.extensionManager.workflow?.activeWorkflow != null;
  });
  await page.evaluate(({ output, anchorId, promptId }) => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((entry: any) => entry.id === "h3-context");
    const panel = document.createElement("div");
    panel.id = "h3-nle-m2555-host-container";
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
    runtime.__m2555HostAnchor = anchor;
    runtime.__m2555HostNative = native;
    runtime.comfyAPI.api.api.dispatchEvent(
      new CustomEvent("executed", {
        detail: { node: anchorId, prompt_id: promptId, output },
      }),
    );
  }, fixture);

  const shell = page.locator("#h3-nle-m2555-host-container");
  await shell.locator('[data-page-id="production"]').click();
  await shell.locator('[data-h3-director-function="clip_editor"]').click();
  await shell
    .getByRole("button", {
      name: "Start authoring from this context",
      exact: true,
    })
    .click();
  await expect(
    shell.getByRole("button", {
      name: "Start authoring from this context",
      exact: true,
    }),
  ).toHaveCount(0);
  await shell.locator('[data-h3-nle-entry="open"]').click();
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  await expect(overlay).toHaveCount(1);
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");

  return {
    overlay,
    successfulTransactions,
    cleanup: async () => {
      page.off("response", observeAuthoring);
      if (await overlay.count())
        await overlay.locator('[data-h3-nle-action="close"]').click();
      if (authoringHandle) {
        const body = encodeAuthoringAction(
          `m2555-host-release-${authoringHandle.slice(-16)}`,
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
        if (runtime.__m2555HostAnchor)
          app.graph.remove(runtime.__m2555HostAnchor);
        if (runtime.__m2555HostNative)
          app.graph.remove(runtime.__m2555HostNative);
        document.getElementById("h3-nle-m2555-host-container")?.remove();
      });
    },
  };
}

async function seekOwnedRuler(ruler: Locator, target: number): Promise<void> {
  if (!Number.isSafeInteger(target) || target < 0 || target > 512)
    throw new Error("bounded host seek target is invalid");
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await ruler.press("Home");
  for (let frame = 0; frame < target; frame += 1)
    await ruler.press("ArrowRight");
  await expect(ruler).toHaveAttribute("aria-valuenow", String(target));
}

test("A55: position survives rebind and unavailable recovery", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.H3_CONTEXT_M25_55_HOST !== "1",
    "explicit M25-55 supplied-host sample required",
  );
  test.setTimeout(180_000);
  const workspace = await openM2555HostWorkspace(page, context);
  const { overlay, successfulTransactions } = workspace;
  try {
    await overlay.locator('[data-h3-nle-pane="assets"]').click();
    await overlay
      .locator(
        '[data-h3-nle-asset="video_1"] [data-h3-nle-control="asset.insert"]',
      )
      .click();
    await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);
    const monitor = overlay.locator('[data-h3-nle-status="monitor"]');
    await expect(monitor).toHaveText("Monitor paused.", { timeout: 30_000 });
    const ruler = overlay.locator('[data-h3-nle-control="transport.seek"]');
    await seekOwnedRuler(ruler, 12);
    const canvas = overlay.locator('[data-h3-nle-canvas="composition"]');
    await expect(canvas).toHaveAttribute("data-h3-nle-presented-frame", "12");
    await canvas.evaluate((element) => {
      const paints: string[] = [];
      (window as any).__m2555HostPaints = paints;
      new MutationObserver(() => {
        const frame = element.getAttribute("data-h3-nle-presented-frame");
        if (frame !== null) paints.push(frame);
      }).observe(element, {
        attributes: true,
        attributeFilter: ["data-h3-nle-presented-frame"],
      });
    });
    const beforeSelection = successfulTransactions.length;
    const body = overlay.locator(
      '[data-h3-nle-clip] [data-h3-nle-control="selection.set"]',
    );
    await body.click();
    await expect
      .poll(() => successfulTransactions.length)
      .toBe(beforeSelection + 1);
    await expect(ruler).toHaveAttribute("aria-valuenow", "12");
    await expect(canvas).toHaveAttribute("data-h3-nle-presented-frame", "12");
    expect(
      await page.evaluate(() => (window as any).__m2555HostPaints as string[]),
    ).not.toContain("0");

    await loseCanvasContext(page, true);
    await expect(monitor).toHaveText(
      "Monitor unavailable: canvas unavailable.",
    );
    await ruler.press("ArrowRight");
    await expect(ruler).toHaveAttribute("aria-valuenow", "13");
    await expect(
      overlay.locator('[data-h3-nle-control="transport.play"]'),
    ).toBeDisabled();
    await loseCanvasContext(page, false);
    await overlay.locator('[data-h3-nle-control="transport.recover"]').click();
    await expect(monitor).toHaveText("Monitor paused.", { timeout: 30_000 });
    await expect(canvas).toHaveAttribute("data-h3-nle-presented-frame", "13");

    await ruler.press("Home");
    await expect(ruler).toHaveAttribute("aria-valuenow", "0");
    const box = await body.boundingBox();
    if (box === null) throw new Error("clip body geometry unavailable");
    await body.dblclick({
      position: { x: Math.max(2, Math.floor(box.width / 2)), y: 8 },
    });
    await expect
      .poll(async () => Number(await ruler.getAttribute("aria-valuenow")))
      .toBeGreaterThan(0);
    const doubleClickTarget = await ruler.getAttribute("aria-valuenow");
    if (doubleClickTarget === null)
      throw new Error("logical playhead value unavailable");
    await expect(canvas).toHaveAttribute(
      "data-h3-nle-presented-frame",
      doubleClickTarget,
    );
  } finally {
    await workspace.cleanup();
  }
});

test("A55: empty transition and owned navigation", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.H3_CONTEXT_M25_55_HOST !== "1",
    "explicit M25-55 supplied-host sample required",
  );
  test.setTimeout(180_000);
  const workspace = await openM2555HostWorkspace(page, context);
  const { overlay } = workspace;
  try {
    await expect(
      overlay.locator('[data-h3-nle-empty-origin="0"]'),
    ).toBeVisible();
    await overlay.locator('[data-h3-nle-pane="assets"]').click();
    await overlay
      .locator(
        '[data-h3-nle-asset="video_1"] [data-h3-nle-control="asset.insert"]',
      )
      .click();
    await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);
    const ruler = overlay.locator('[data-h3-nle-control="transport.seek"]');
    await seekOwnedRuler(ruler, 12);
    await loseCanvasContext(page, true);
    await expect(overlay.locator('[data-h3-nle-status="monitor"]')).toHaveText(
      "Monitor unavailable: canvas unavailable.",
    );
    await page.evaluate(() => {
      const cancelOwnedKeys = (event: KeyboardEvent) => {
        if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
          event.preventDefault();
      };
      (window as any).__m2555CancelOwnedKeys = cancelOwnedKeys;
      document.addEventListener("keydown", cancelOwnedKeys);
    });
    await ruler.press("ArrowRight");
    await expect(ruler).toHaveAttribute("aria-valuenow", "13");
    await ruler.press("Home");
    await expect(ruler).toHaveAttribute("aria-valuenow", "0");
    await loseCanvasContext(page, false);
    await overlay.locator('[data-h3-nle-control="transport.recover"]').click();
    await expect(overlay.locator('[data-h3-nle-status="monitor"]')).toHaveText(
      "Monitor paused.",
      { timeout: 30_000 },
    );

    const body = overlay.locator(
      '[data-h3-nle-clip] [data-h3-nle-control="selection.set"]',
    );
    await body.click();
    await overlay.locator('[data-h3-nle-control="clip.remove"]').click();
    await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(0);
    await expect(
      overlay.locator('[data-h3-nle-empty-origin="0"]'),
    ).toBeVisible();
    await expect(
      overlay.locator('[data-h3-nle-control="transport.seek"]'),
    ).toHaveCount(0);
  } finally {
    await page.evaluate(() => {
      const listener = (window as any).__m2555CancelOwnedKeys as
        EventListener | undefined;
      if (listener) document.removeEventListener("keydown", listener);
      delete (window as any).__m2555CancelOwnedKeys;
    });
    await workspace.cleanup();
  }
});

test("NLE workspace opens from the shipped tab and edits a real bound VIDEO", async ({
  page,
  context,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_NLE_WORKSPACE_HOST !== "1",
    "explicit model-free supplied-host sample required",
  );
  test.setTimeout(180_000);
  if (!hostUrl) throw new Error("supplied host required");
  const configured = process.env.H3_CONTEXT_NLE_WORKSPACE_FIXTURE;
  if (!configured) throw new Error("owned fixture required");
  const fixture = JSON.parse(
    await readFile(resolve(repositoryRoot, configured), "utf8"),
  );
  if (fixture.schema !== "NleWorkspaceHostFixtureV1")
    throw new Error("fixture contract mismatch");
  expect(fixture.qualification).toMatchObject({
    schema: "M25RealRuntimeQualificationV1",
    normalStartupMediaActive: true,
    activeModulesInstalledOnly: true,
    backupModulesExcluded: true,
    canonicalNodesRegistered: true,
    candidateBundleSha256: candidateBundle?.sha256,
  });
  let queueCalls = 0;
  let authoringHandle: string | undefined;
  let audioObserver:
    Awaited<ReturnType<typeof startProcessAudioObserver>> | undefined;
  const actions: string[] = [];
  const receipts: TimelineReceipt[] = [];
  page.on("response", (response) => {
    if (
      !new URL(response.url()).pathname.includes(
        "/authoring/media-source-leases",
      )
    )
      return;
    const request = response.request().postDataJSON();
    console.log(
      JSON.stringify({
        media: request.operation,
        status: response.status(),
        revision: request.timelineRevision ?? null,
      }),
    );
  });
  page.on("response", async (response) => {
    if (response.url().includes("/h3-context/") && response.status() >= 400) {
      const body = await response.json().catch(() => null);
      const code = body?.code ?? body?.error?.code ?? body?.reason;
      console.log(
        JSON.stringify({
          hostRefusal: response.status(),
          route: new URL(response.url()).pathname.endsWith(
            "/media-source-lease",
          )
            ? "media_source_lease"
            : "other_owned_route",
          code:
            typeof code === "string" && /^[a-z0-9_]{1,100}$/.test(code)
              ? code
              : "unclassified",
        }),
      );
    }
  });
  page.on("response", async (response) => {
    if (
      new URL(response.url()).pathname.replace(
        /^\/api(?=\/h3-context\/)/,
        "",
      ) !== "/h3-context/v1/authoring/action"
    )
      return;
    if (
      response.request().postDataJSON()?.action ===
        "apply_timeline_transaction" &&
      response.status() === 200
    ) {
      receipts.push(decodeTimelineReceipt(await response.json()));
    }
    if (
      response.request().postDataJSON()?.action ===
        "create_authoring_workspace" &&
      response.status() === 201
    ) {
      const body = await response.json();
      if (typeof body.workspace_handle === "string")
        authoringHandle = body.workspace_handle;
    }
  });
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname.replace(/^\/api(?=\/)/, "");
    if (path === "/prompt" && request.method() === "POST") queueCalls++;
    if (path === "/h3-context/v1/authoring/action") {
      const body = request.postDataJSON() as { action?: string };
      if (body.action) actions.push(body.action);
    }
  });
  const injectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() =>
    (window as any).comfyAPI?.app?.app?.extensionManager
      ?.getSidebarTabs?.()
      .some((tab: any) => tab.id === "h3-context"),
  );
  await assertCandidateBundleInjection(page, context, injectionCount);
  await page.waitForFunction(() => {
    const app = (window as any).comfyAPI.app.app;
    return app.extensionManager.workflow?.activeWorkflow != null;
  });
  await page.evaluate(() => {
    const app = (window as any).comfyAPI.app.app;
    const tab = app.extensionManager
      .getSidebarTabs()
      .find((entry: any) => entry.id === "h3-context");
    const panel = document.createElement("div");
    panel.id = "h3-nle-host-container";
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
    (window as any).__nleHostIdentity = {
      workflow: app.extensionManager.workflow.activeWorkflow,
      tabs: app.extensionManager.workflow.openWorkflows.length,
    };
  });
  const shell = page.locator("#h3-nle-host-container");
  await shell.locator('[data-page-id="production"]').click();
  await shell.locator('[data-h3-director-function="clip_editor"]').click();
  const overlay = page.locator('[data-h3-nle-surface="overlay_v1"]');
  const open = shell.locator('[data-h3-nle-entry="open"]');
  for (let attempt = 0; attempt < 2; attempt++) {
    await open.click();
    await expect(overlay).toHaveCount(1);
    await expect(overlay.locator("h2")).toBeFocused();
    await overlay.locator('[data-h3-nle-action="close"]').click();
    await expect(overlay).toHaveCount(0);
    await expect(open).toBeFocused();
  }
  expect(actions).toEqual([]);
  await page.evaluate(({ output, anchorId, promptId }) => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const node = runtime.LiteGraph.createNode(
      "comfyui_h3_context.H3Context.ProductShell",
    );
    const native = runtime.LiteGraph.createNode("MiniMaxH3ReferenceToVideo");
    if (!node || app.graph.getNodeById(anchorId))
      throw new Error("owned anchor unavailable");
    if (!native) throw new Error("supplied native H3 registration unavailable");
    node.id = anchorId;
    app.graph.add(node);
    app.graph.add(native);
    runtime.__nleHostAnchor = node;
    runtime.__nleHostNative = native;
    runtime.comfyAPI.api.api.dispatchEvent(
      new CustomEvent("executed", {
        detail: { node: anchorId, prompt_id: promptId, output },
      }),
    );
  }, fixture);
  const graphBefore = await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const ownedProjection = () =>
      [runtime.__nleHostAnchor, runtime.__nleHostNative].map((node) => ({
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
    runtime.__nleHostOwnedProjection = ownedProjection;
    runtime.__nleHostOwnedBefore = JSON.stringify(ownedProjection());
    return app.graph.serialize();
  });
  try {
    await shell.locator('[data-page-id="production"]').click();
    await shell.locator('[data-h3-director-function="clip_editor"]').click();
    await shell
      .getByRole("button", {
        name: "Start authoring from this context",
        exact: true,
      })
      .click();
    await expect(
      shell.getByRole("button", {
        name: "Start authoring from this context",
        exact: true,
      }),
    ).toHaveCount(0);
    await open.click();
    await expect(
      overlay.locator('[data-h3-nle-region="timeline"]'),
    ).toBeVisible();
    await expect(
      overlay.locator('[data-h3-nle-status="timeline"]'),
    ).toHaveAttribute("data-h3-nle-authoring", "ready");
    // M25-44: four areas in the reference proportions and three splitters that move by a real
    // pointer drag, on the supplied host at 1440 x 900.
    const { standard, splitterMoves } = await expectStandardReferenceShell(
      page,
      overlay,
    );
    await overlay.locator('[data-h3-nle-pane="assets"]').click();
    await overlay
      .locator(
        '[data-h3-nle-card-index="2"] [data-h3-nle-control="asset.insert"]',
      )
      .click();
    await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);
    await expect(
      overlay.locator('[data-h3-nle-control="transport.play"]'),
    ).toBeEnabled();
    const pixels = () =>
      overlay
        .locator('[data-h3-nle-canvas="composition"]')
        .evaluate((canvas) => {
          const target = canvas as HTMLCanvasElement;
          const data = target
            .getContext("2d")!
            .getImageData(0, 0, target.width, target.height).data;
          let checksum = 2166136261,
            colors = 0;
          for (let index = 0; index < data.length; index += 64) {
            checksum = Math.imul(checksum ^ data[index]!, 16777619);
            if (data[index]! + data[index + 1]! + data[index + 2]! > 40)
              colors++;
          }
          return { checksum: checksum >>> 0, colors };
        });
    const firstPixels = await pixels();
    expect(firstPixels.colors).toBeGreaterThan(100);
    const ruler = overlay.locator('[data-h3-nle-control="transport.seek"]');
    const seekFrame = async (target: number) => {
      if (!Number.isSafeInteger(target) || target < 0 || target > 512)
        throw new Error("bounded host seek target is invalid");
      // IMPORTANT: this control is an ARIA slider, not an input; fill() cannot exercise its keyboard seek contract.
      await expect(ruler).toHaveAttribute("aria-disabled", "false");
      await ruler.press("Home");
      for (let frame = 0; frame < target; frame += 1)
        await ruler.press("ArrowRight");
      await expect(ruler).toHaveAttribute("aria-valuenow", String(target));
    };
    await seekFrame(12);
    await expect
      .poll(async () => (await pixels()).checksum)
      .not.toBe(firstPixels.checksum);
    await ruler.press("Home");
    await expect(ruler).toHaveAttribute("aria-valuenow", "0");
    await expect
      .poll(async () => (await pixels()).checksum)
      .toBe(firstPixels.checksum);
    const browser = context.browser();
    if (!browser) throw new Error("test browser missing");
    const browserCdp = await browser.newBrowserCDPSession();
    const info = await browserCdp.send("SystemInfo.getProcessInfo");
    const pid = info.processInfo.find((row) => row.type === "browser")?.id;
    if (!pid) throw new Error("test browser PID missing");
    audioObserver = await startProcessAudioObserver(
      repositoryRoot,
      pid,
      resolve(repositoryRoot, fixture.observerRelative),
      fixture.observerSha256,
    );
    await overlay.locator('[data-h3-nle-control="transport.play"]').click();
    await expect(
      overlay.locator('[data-h3-nle-status="audio"]'),
    ).toHaveAttribute("data-h3-nle-audio-state", "following");
    await page.waitForTimeout(1500);
    await overlay.locator('[data-h3-nle-control="transport.pause"]').click();
    await expect(
      overlay.locator('[data-h3-nle-status="audio"]'),
    ).toHaveAttribute("data-h3-nle-audio-state", "suspended");
    const pausedAt = await page.evaluate(
      () => performance.timeOrigin + performance.now(),
    );
    await page.waitForTimeout(450);
    const audio = await audioObserver.stop();
    audioObserver = undefined;
    expect(audio.selector).toBe("include_test_browser_process_tree");
    expect(audio.rawAudioRetained || audio.microphoneOpened).toBe(false);
    expect(audio.browserExecutableSha256).toBe(fixture.browserExecutableSha256);
    expect(audio.onsets.length).toBeGreaterThanOrEqual(2);
    const stopped = audio.packets.filter((row) => row.time > pausedAt + 250);
    expect(stopped.length).toBeGreaterThan(0);
    expect(stopped.every((row) => row.pcm16Peak === 0)).toBe(true);
    await overlay
      .locator('[data-h3-nle-clip] [data-h3-nle-control="selection.set"]')
      .click();
    await expect.poll(() => receipts.length).toBe(2);
    for (const edge of ["end", "start"] as const) {
      await expect(
        overlay.locator('[data-h3-nle-status="timeline"]'),
      ).toHaveAttribute("data-h3-nle-authoring", "ready");
      const before = receipts.at(-1)!.snapshot.clips[0]!;
      const count = receipts.length;
      const grip = overlay
        .locator(`[data-h3-nle-trim-edge="${edge}"]:not([hidden])`)
        .filter({ visible: true })
        .first();
      const box = await grip.boundingBox();
      if (!box) throw new Error("trim_grip_not_visible");
      const x = box.x + box.width / 2,
        y = box.y + box.height / 2;
      await page.mouse.move(x, y);
      await page.mouse.down();
      await page.mouse.move(x + (edge === "start" ? 12 : -12), y, { steps: 3 });
      expect(receipts.length).toBe(count);
      await page.mouse.up();
      await expect.poll(() => receipts.length).toBe(count + 1);
      const accepted = receipts.at(-1)!.snapshot.clips[0]!;
      expect(accepted.durationFrames).toBeLessThan(before.durationFrames);
      await expect(
        overlay.locator('[data-h3-nle-status="timeline"]'),
      ).toHaveAttribute("data-h3-nle-authoring", "ready");
      await overlay.locator('[data-h3-nle-control="history.undo"]').click();
      await expect.poll(() => receipts.length).toBe(count + 2);
      expect(receipts.at(-1)!.snapshot.clips[0]).toEqual(before);
      await expect(
        overlay.locator('[data-h3-nle-status="timeline"]'),
      ).toHaveAttribute("data-h3-nle-authoring", "ready");
      await overlay.locator('[data-h3-nle-control="history.redo"]').click();
      await expect.poll(() => receipts.length).toBe(count + 3);
      expect(receipts.at(-1)!.snapshot.clips[0]).toEqual(accepted);
    }
    await expect(
      overlay.locator('[data-h3-nle-status="timeline"]'),
    ).toHaveAttribute("data-h3-nle-authoring", "ready");
    await expect(overlay.locator('[data-h3-nle-status="monitor"]')).toHaveText(
      "Monitor paused.",
    );
    await seekFrame(receipts.at(-1)!.snapshot.clips[0]!.startFrame);
    console.log(
      JSON.stringify({
        trimmed: receipts.at(-1)!.snapshot.clips.map((clip) => ({
          start: clip.startFrame,
          duration: clip.durationFrames,
          source: clip.sourceStartFrame,
        })),
        monitor: await overlay
          .locator('[data-h3-nle-status="monitor"]')
          .textContent(),
        frame: await overlay
          .locator('[data-h3-nle-control="transport.seek"]')
          .getAttribute("aria-valuetext"),
      }),
    );
    try {
      await expect(ruler).toHaveAttribute(
        "aria-valuetext",
        /^\d{2}:\d{2}:\d{2}:\d{2}$/,
      );
    } catch (error) {
      console.log(
        JSON.stringify({
          monitorAfterSeek: await overlay
            .locator('[data-h3-nle-status="monitor"]')
            .textContent(),
          audioAfterSeek: await overlay
            .locator('[data-h3-nle-status="audio"]')
            .textContent(),
        }),
      );
      throw error;
    }
    await expect.poll(async () => (await pixels()).colors).toBeGreaterThan(100);
    // M25-44: the final-video card lives in the chrome bar's Export popover.
    await openExportPanel(overlay);
    const output = overlay.getByRole("region", {
      name: "Final video",
      exact: true,
    });
    await output
      .getByRole("button", { name: "Render final video", exact: true })
      .click();
    await expect(output.getByRole("status")).toHaveText("Video ready", {
      timeout: 90_000,
    });
    await output
      .getByRole("button", { name: "Preview output", exact: true })
      .click();
    const preview = output.getByLabel("Final video preview", { exact: true });
    await preview.evaluate(async (element) => {
      const video = element as HTMLVideoElement;
      video.currentTime = 0.5;
      await video.play();
    });
    await expect
      .poll(() =>
        preview.evaluate((element) => (element as HTMLVideoElement).readyState),
      )
      .toBeGreaterThanOrEqual(2);
    expect(
      await preview.evaluate(
        (element) => (element as HTMLVideoElement).videoWidth,
      ),
    ).toBeGreaterThan(0);
    await output
      .getByRole("button", { name: "Close preview", exact: true })
      .click();
    await overlay.locator('[data-h3-nle-action="close"]').click();
    await expect(overlay).toHaveCount(0);
    await expect(open).toBeFocused();
    // M25-44: the narrow tier at 720 x 480 on the supplied host, opened after the viewport change
    // so the explicit open re-clamps the retained bounds.
    await page.setViewportSize({ width: 720, height: 480 });
    await open.click();
    await expect(overlay).toHaveCount(1);
    const narrow = await expectNarrowReferenceShell(overlay);
    await overlay.locator('[data-h3-nle-action="close"]').click();
    await expect(overlay).toHaveCount(0);
    await page.setViewportSize({ width: 1440, height: 900 });
    console.log(
      JSON.stringify({
        referenceShell: {
          standard: summarizeReferenceShell(standard),
          splitterMoves,
          narrow: summarizeReferenceShell(narrow),
        },
      }),
    );
    expect(
      actions.filter((action) => action === "create_authoring_workspace"),
    ).toHaveLength(1);
    expect(
      actions.filter((action) => action === "apply_timeline_transaction"),
    ).toHaveLength(8);
    expect(queueCalls).toBe(0);
    expect(context.pages()).toHaveLength(1);
    expect(
      await page.evaluate(() => {
        const app = (window as any).comfyAPI.app.app;
        const captured = (window as any).__nleHostIdentity;
        return {
          workflow:
            app.extensionManager.workflow.activeWorkflow === captured.workflow,
          tabs:
            app.extensionManager.workflow.openWorkflows.length ===
            captured.tabs,
          h3Tabs: app.extensionManager
            .getSidebarTabs()
            .filter((tab: any) => tab.id === "h3-context").length,
        };
      }),
    ).toEqual({ workflow: true, tabs: true, h3Tabs: 1 });
    const after = await page.evaluate(() => {
      const runtime = window as any;
      const app = runtime.comfyAPI.app.app;
      return {
        graph: app.graph.serialize(),
        ownedIds: [runtime.__nleHostAnchor.id, runtime.__nleHostNative.id].map(
          String,
        ),
        ownedStable:
          JSON.stringify(runtime.__nleHostOwnedProjection()) ===
          runtime.__nleHostOwnedBefore,
        sidebarCensus: app.extensionManager
          .getSidebarTabs()
          .map((tab: any) => tab.id)
          .sort(),
      };
    });
    expect(after.ownedStable).toBe(true);
    const surroundings = diffGraphSurroundings({
      beforeValue: graphBefore,
      afterValue: after.graph,
      reference: {
        ownedNodeIds: after.ownedIds,
        ownedLinkIds: [],
        anchorNodeId: fixture.anchorId,
        ownedProjectionEqual: after.ownedStable,
      },
    });
    const qualificationReceipt = {
      ...fixture.qualification,
      workflowIdentityStable: true,
      openTabsStable: true,
      ownedProjectionStable: true,
      queueCalls,
      acceptedTransactions: receipts.length,
      actualDecodedVideo: true,
      processAudioOnsets: audio.onsets.length,
      audioSilentAfterPause: true,
      bothEdgeTrimsAndUndoRedo: true,
      actualFinalRenderAndPreviewPlayback: true,
      sidebarCensus: after.sidebarCensus,
      surroundings,
    };
    // The host lane deliberately deletes attachments; retain only this content-free receipt.
    console.log(JSON.stringify({ nleHostQualification: qualificationReceipt }));
    await testInfo.attach("nle-workspace-host-qualification", {
      contentType: "application/json",
      body: JSON.stringify(qualificationReceipt),
    });
  } finally {
    await audioObserver?.stop();
    if (await overlay.count())
      await overlay.locator('[data-h3-nle-action="close"]').click();
    if (authoringHandle) {
      const body = encodeAuthoringAction(
        `nle-host-release-${authoringHandle.slice(-16)}`,
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
      if (runtime.__nleHostAnchor) app.graph.remove(runtime.__nleHostAnchor);
      if (runtime.__nleHostNative) app.graph.remove(runtime.__nleHostNative);
      document.getElementById("h3-nle-host-container")?.remove();
    });
  }
});
