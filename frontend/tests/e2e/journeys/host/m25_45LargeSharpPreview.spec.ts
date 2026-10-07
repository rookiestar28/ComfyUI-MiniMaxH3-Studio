import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import {
  dragSplitter,
  expectFit,
  near,
  settledPicture,
  summarizePicture,
} from "../../helpers/nleMonitorPicture";
import {
  assertCandidateBundleInjection,
  candidateBundle,
  candidateInjectionCount,
  hostUrl,
  repositoryRoot,
} from "../../host/candidate";
import { test, expect } from "../../host/fixture";

/**
 * M25-45 AC45-08: the monitor's picture, one real proxy lease, and the transport, full screen and
 * top layer, on the supplied host.
 *
 * The criterion names all four, and the third row exists because reading it against the spec found
 * only the first two (B-M2545-43). The top-layer half is the one that cannot be moved to the
 * hermetic lane and be worth anything: the overlay enters the top layer precisely because installed
 * packs paint fixed elements at the viewport's edges, and the hermetic shell has no packs.
 *
 * What a host row may assert is fixed by TEST_SOP section 4: the owned projection, the captured
 * workflow identity, the open tab count and the queue-call count, with everything else a bucketed
 * path diff. Nothing here requires a quiet canvas or a clean profile, and no foreign callback is
 * wrapped or blocked.
 *
 * The fit rows carry no expected sizes. `expectFit` states the rule against the composition the
 * picture area itself declares (`data-h3-nle-picture="WxH"`), and the backing is checked against
 * the picture the host actually laid out -- so the numbers come from the host rather than from a
 * formula this file re-implements, which is the difference between observing the host and
 * predicting it. The same reader runs in the hermetic journey, so the two lanes cannot answer
 * slightly different questions about the same pixels.
 *
 * Every row scans the host log and fails closed without one: a row that reports PASS without
 * having scanned is the same false PASS an owned handler error produces.
 */

test.use({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
});

const OVERLAY = '[data-h3-nle-surface="overlay_v1"]';
/** The item's own cap. A backing above it would mean the shipped cap did not apply on the host. */
const CAP_EDGE = 1280;
const CAP_PIXELS = 921_600;

type HostState = Readonly<{
  ownedBefore: string;
  tabsBefore: number;
  workflowBefore: unknown;
}>;

/** Open the shipped sidebar tab, publish a queue-free Context, and reach the clip editor. */
async function openWorkspace(
  page: import("../../host/fixture").Page,
  context: import("../../host/fixture").BrowserContext,
  fixture: { output: unknown; anchorId: number; promptId: string },
): Promise<{ overlay: ReturnType<typeof page.locator>; state: HostState }> {
  const injectionCount = candidateInjectionCount(context);
  await page.goto(hostUrl!, { waitUntil: "domcontentloaded" });
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
  });
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
    runtime.__h3Anchor = node;
    runtime.__h3Native = native;
    runtime.comfyAPI.api.api.dispatchEvent(
      new CustomEvent("executed", {
        detail: { node: anchorId, prompt_id: promptId, output },
      }),
    );
  }, fixture);

  const state = await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const ownedProjection = () =>
      [runtime.__h3Anchor, runtime.__h3Native].map((node: any) => ({
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
    runtime.__h3OwnedProjection = ownedProjection;
    return {
      ownedBefore: JSON.stringify(ownedProjection()),
      tabsBefore: app.extensionManager.workflow.openWorkflows.length,
      workflowBefore:
        app.extensionManager.workflow.activeWorkflow?.path ?? null,
    };
  });

  const shell = page.locator("#h3-nle-host-container");
  await shell.locator('[data-page-id="production"]').click();
  await shell.locator('[data-h3-director-function="clip_editor"]').click();
  await shell
    .getByRole("button", {
      name: "Start authoring from this context",
      exact: true,
    })
    .click();
  await shell.locator('[data-h3-nle-entry="open"]').click();
  const overlay = page.locator(OVERLAY);
  await expect(
    overlay.locator('[data-h3-nle-region="timeline"]'),
  ).toBeVisible();
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");
  return { overlay, state };
}

/** TEST_SOP section 4's list: the owned projection, the identity, the tabs and the queue calls. */
async function expectOwnedIdentityUnchanged(
  page: import("../../host/fixture").Page,
  state: HostState,
  queueCalls: number,
): Promise<void> {
  const after = await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    return {
      owned: JSON.stringify(runtime.__h3OwnedProjection()),
      tabs: app.extensionManager.workflow.openWorkflows.length,
      workflow: app.extensionManager.workflow.activeWorkflow?.path ?? null,
    };
  });
  expect(after.owned).toBe(state.ownedBefore);
  expect(after.tabs).toBe(state.tabsBefore);
  expect(after.workflow).toBe(state.workflowBefore);
  // A preview is not an execution. The monitor may never queue.
  expect(queueCalls).toBe(0);
}

async function loadFixture(): Promise<{
  output: unknown;
  anchorId: number;
  promptId: string;
}> {
  const configured = process.env.H3_CONTEXT_NLE_WORKSPACE_FIXTURE;
  if (!configured) throw new Error("owned fixture required");
  const fixture = JSON.parse(
    await readFile(resolve(repositoryRoot, configured), "utf8"),
  );
  if (fixture.schema !== "NleWorkspaceHostFixtureV1")
    throw new Error("fixture contract mismatch");
  expect(fixture.qualification).toMatchObject({
    schema: "M25RealRuntimeQualificationV1",
    candidateBundleSha256: candidateBundle?.sha256,
  });
  return fixture;
}

test("the picture fits the composition on the supplied host, in both limiting directions", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.H3_CONTEXT_M25_45_HOST !== "1",
    "explicit model-free supplied-host sample required",
  );
  test.setTimeout(240_000);
  if (!hostUrl) throw new Error("supplied host required");
  let queueCalls = 0;
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname.replace(/^\/api(?=\/)/, "");
    if (path === "/prompt" && request.method() === "POST") queueCalls++;
  });

  const fixture = await loadFixture();
  const { overlay, state } = await openWorkspace(page, context, fixture);
  const graphBefore = await page.evaluate(() =>
    (window as any).comfyAPI.app.app.graph.serialize(),
  );

  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await overlay
    .locator(
      '[data-h3-nle-card-index="2"] [data-h3-nle-control="asset.insert"]',
    )
    .click();
  await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);

  // Width-limited: the host's default layout gives the monitor a wide, short area.
  const first = await settledPicture(page, "host default layout");
  expectFit(first, "host default layout");
  expect(first.area.width).toBeLessThanOrEqual(
    first.area.height * first.aspect + 1,
  );
  near(first.canvas.width, first.area.width);
  // The item's point, on the host's own canvas: a large picture, and a backing that follows it up
  // to the composition and the shipped cap. Observed, never predicted -- the composition is what
  // the picture area declares and the canvas is what the host laid out.
  expect(first.canvas.width).toBeGreaterThanOrEqual(700);
  // GUARD: this exact equality holds only while the composition is narrower than the picture, so
  // the scale saturates at 1 and the backing is the composition's own size. `computePreviewSize`
  // rounds a scaled backing to an EVEN number of pixels, so on a composition wider than the
  // picture this expectation is off by one about half the time -- a red row that says nothing
  // about the product. Copy it to a 1280-wide composition and it must become an even-aware bound.
  expect(first.backing.width).toBe(
    Math.min(first.composition.width, Math.round(first.canvas.width)),
  );
  expect(first.backing.width).toBeLessThanOrEqual(CAP_EDGE);
  expect(first.backing.height).toBeLessThanOrEqual(CAP_EDGE);
  expect(first.backing.width * first.backing.height).toBeLessThanOrEqual(
    CAP_PIXELS,
  );

  // A taller area that is still width-limited leaves the picture alone and letterboxes.
  await dragSplitter(page, "top_timeline", 100);
  const taller = await settledPicture(page, "host S3 down");
  expectFit(taller, "host S3 down");
  near(taller.canvas.width, first.canvas.width);
  expect(taller.area.height - taller.canvas.height).toBeGreaterThan(
    first.area.height - first.canvas.height,
  );

  // Height-limited: the top band at its minimum makes the monitor short and wide.
  await dragSplitter(page, "top_timeline", -400);
  const short = await settledPicture(page, "host top band minimum");
  expectFit(short, "host top band at its minimum");
  expect(short.area.height).toBeLessThan(short.area.width / short.aspect + 1);
  near(short.canvas.height, short.area.height);
  near(short.canvas.width, short.area.height * short.aspect);
  // Centred, with the letterbox on the other axis only.
  near(
    short.canvas.x - short.area.x,
    (short.area.width - short.canvas.width) / 2,
  );
  // A wider monitor changes nothing while the area is height-limited.
  await dragSplitter(page, "bin_monitor", -60);
  const wider = await settledPicture(page, "host height-limited, wider");
  expectFit(wider, "host height-limited, wider pane");
  near(wider.canvas.height, short.canvas.height);

  console.log(
    JSON.stringify({
      fixtures: {
        width_limited: summarizePicture(first),
        width_limited_taller: summarizePicture(taller),
        height_limited: summarizePicture(short),
        height_limited_wider: summarizePicture(wider),
      },
    }),
  );

  await expectOwnedIdentityUnchanged(page, state, queueCalls);
  const after = await page.evaluate(() => {
    const runtime = window as any;
    return {
      graph: runtime.comfyAPI.app.app.graph.serialize(),
      // The diff addresses nodes by their serialized string keys, not by LiteGraph's numeric ids.
      ownedIds: [String(runtime.__h3Anchor.id), String(runtime.__h3Native.id)],
    };
  });
  // Everything the canvas did that this repository did not write is bucketed and recorded, never
  // required to be absent: a host rewrites its own layout, metadata and randomized widgets between
  // two interactions, and so does every pack installed beside this one. Only `owned` may move.
  const surroundings = diffGraphSurroundings({
    beforeValue: graphBefore,
    afterValue: after.graph,
    reference: {
      ownedNodeIds: after.ownedIds,
      ownedLinkIds: [],
      anchorNodeId: String(fixture.anchorId),
      ownedProjectionEqual: true,
    },
  });
  console.log(JSON.stringify({ surroundings: surroundings.counts }));
  expect(surroundings.counts.owned).toBe(0);
});

test("a real proxy lease opens and releases without touching the owned projection", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.H3_CONTEXT_M25_45_HOST !== "1",
    "explicit model-free supplied-host sample required",
  );
  test.setTimeout(240_000);
  if (!hostUrl) throw new Error("supplied host required");
  let queueCalls = 0;
  const leases: { operation: string; status: number }[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname.replace(/^\/api(?=\/)/, "");
    if (path === "/prompt" && request.method() === "POST") queueCalls++;
  });
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname.replace(/^\/api(?=\/)/, "");
    if (!path.includes("/authoring/media-source-leases")) return;
    const body = response.request().postDataJSON() as { operation?: string };
    leases.push({
      operation: String(body?.operation ?? "unknown"),
      status: response.status(),
    });
  });

  const fixture = await loadFixture();
  const { overlay, state } = await openWorkspace(page, context, fixture);

  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await overlay
    .locator(
      '[data-h3-nle-card-index="2"] [data-h3-nle-control="asset.insert"]',
    )
    .click();
  await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);

  // The lease is what makes a picture out of a real source, so the picture is the proof it opened:
  // a backing the host sized to the composition, reached only once the media runtime answered.
  const picture = await settledPicture(page, "host lease presented");
  expect(picture.backing.width).toBeGreaterThan(0);
  await expect(
    overlay.locator('[data-h3-nle-control="transport.play"]'),
  ).toBeEnabled();
  expect(leases.some((item) => item.status >= 200 && item.status < 300)).toBe(
    true,
  );

  // Closing releases it. The owned projection, the identity, the tabs and the queue are untouched
  // throughout: a preview never queues and never writes the graph.
  await overlay.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  await expectOwnedIdentityUnchanged(page, state, queueCalls);

  console.log(
    JSON.stringify({
      lease_calls: leases,
      presented: summarizePicture(picture),
    }),
  );
});

test("the transport, full screen and the top layer behave on the supplied host", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.H3_CONTEXT_M25_45_HOST !== "1",
    "explicit model-free supplied-host sample required",
  );
  test.setTimeout(240_000);
  if (!hostUrl) throw new Error("supplied host required");
  let queueCalls = 0;
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname.replace(/^\/api(?=\/)/, "");
    if (path === "/prompt" && request.method() === "POST") queueCalls++;
  });

  const fixture = await loadFixture();
  const { overlay, state } = await openWorkspace(page, context, fixture);
  const control = (id: string) =>
    overlay.locator(`[data-h3-nle-control="${id}"]`);
  const status = overlay.locator('[data-h3-nle-status="monitor"]');

  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await overlay
    .locator(
      '[data-h3-nle-card-index="2"] [data-h3-nle-control="asset.insert"]',
    )
    .click();
  await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);
  const shown = await settledPicture(page, "host transport presented");
  await expect(status).toHaveText("Monitor paused.", { timeout: 60_000 });

  // THE TOP LAYER, which only a real host can actually test. `enterTopLayer` puts the overlay in
  // the top layer with `popover="manual"`, and the reason that exists is packs: the dialog opens at
  // the viewport's edges where an installed pack's own fixed elements would otherwise paint over
  // it. So the assertion is not "the attribute is set" -- it is that the topmost element at the
  // picture's own centre belongs to this overlay, on a canvas that has every pack the owner runs
  // installed beside it. A browser without `showPopover` is a bounded refusal, not a failure.
  const layer = await page.evaluate(
    ({ overlay: selector, x, y }) => {
      const dialog = document.querySelector<HTMLElement>(selector)!;
      // The element that enters the top layer is the ROOT `NleOverlay` appends to `document.body`
      // and calls `showPopover()` on -- `[data-h3-nle-root]`. The surface is a descendant of it,
      // so reading `popover` off the surface answers null however well the product behaves.
      // Measured on the supplied host before this was corrected: supported true, popover null.
      const root =
        dialog.closest<HTMLElement>("[data-h3-nle-root]") ??
        document.querySelector<HTMLElement>("[data-h3-nle-root]") ??
        dialog;
      const hit = document.elementFromPoint(x, y);
      // GUARD: `:popover-open` is only a valid selector where the popover API exists, and
      // `matches` THROWS on an invalid one rather than returning false. Evaluating it
      // unconditionally would turn the bounded-refusal branch -- the one case this whole shape
      // exists to tolerate -- into an exception inside the page. Ask only when supported.
      const supported = typeof (root as any).showPopover === "function";
      return {
        supported,
        popover: root.getAttribute("popover"),
        open: supported ? root.matches(":popover-open") : null,
        rootIsSurface: root === dialog,
        hitInsideOverlay: hit !== null && root.contains(hit),
        hitTag: hit?.tagName ?? null,
      };
    },
    {
      overlay: OVERLAY,
      x: Math.round(shown.canvas.x + shown.canvas.width / 2),
      y: Math.round(shown.canvas.y + shown.canvas.height / 2),
    },
  );
  expect(layer.hitInsideOverlay).toBe(true);
  if (layer.supported) {
    expect(layer.popover).toBe("manual");
    expect(layer.open).toBe(true);
  }

  // THE TRANSPORT, driven by the keyboard the monitor claims to own, against real decoded media.
  const seek = control("transport.seek");
  await seek.focus();
  const start = Number(await seek.inputValue());
  await page.keyboard.press(".");
  await expect
    .poll(async () => Number(await seek.inputValue()))
    .toBe(start + 1);
  await page.keyboard.press(",");
  await expect.poll(async () => Number(await seek.inputValue())).toBe(start);
  await page.keyboard.press(" ");
  await expect(status).toHaveText("Monitor playing.", { timeout: 60_000 });
  await page.keyboard.press(" ");
  await expect(status).toHaveText("Monitor paused.", { timeout: 60_000 });

  // FULL SCREEN: it either takes the monitor or refuses in words, and Escape belongs to full
  // screen before it belongs to the dialog. Both branches are real outcomes on a host -- a headed
  // Chromium under automation may decline the request -- so neither is a skip, and the overlay
  // must survive whichever happens.
  //
  // GUARD: `requestFullscreen()` is ASYNCHRONOUS and the product deliberately does not mirror its
  // result -- `fullscreenchange` owns the truth (B-M2545-31). Reading `document.fullscreenElement`
  // straight after the click therefore samples the state BEFORE the grant lands and reports every
  // outcome as a refusal. Measured on the supplied host: the read said null, this branch skipped
  // Escape, and the monitor went full screen behind the test -- Playwright then timed out after
  // 60 s on the close control, with the composition canvas, by that point 1420 x 799 over a
  // 1440 x 900 viewport, intercepting its pointer events. Wait for the outcome; never sample it.
  await control("transport.fullscreen").click();
  const owned = await page
    .waitForFunction(
      (selector) => {
        const element = document.fullscreenElement;
        if (element === null) return false;
        const surface = document.querySelector(selector);
        return (
          surface !== null &&
          surface.contains(element) &&
          element.classList.contains("h3-nle-monitor")
        );
      },
      OVERLAY,
      { timeout: 10_000 },
    )
    .then(() => true)
    .catch(() => false);
  if (owned) {
    await expect(control("transport.fullscreen")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await page.keyboard.press("Escape");
    await expect
      .poll(() => page.evaluate(() => document.fullscreenElement !== null))
      .toBe(false);
  } else {
    // A refusal must be ANNOUNCED, in the product's own words. This region renders
    // `notice ?? transport.view[view]`, so it is NEVER empty -- it reads "Fit" when nothing was
    // announced. Asserting only that it is non-empty passes on every outcome, including a request
    // that was silently granted, which is exactly how the race above stayed hidden.
    await expect(
      overlay.locator('[data-h3-nle-status="fullscreen"]'),
    ).toHaveText(/Full screen (was refused|is unavailable)/);
    expect(await page.evaluate(() => document.fullscreenElement !== null)).toBe(
      false,
    );
  }
  // The Escape that left full screen never reached the dialog, and the monitor is still live.
  await expect(page.locator(OVERLAY)).toBeVisible();
  await expect(status).toHaveText("Monitor paused.", { timeout: 60_000 });

  await overlay.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(OVERLAY)).toHaveCount(0);
  await expectOwnedIdentityUnchanged(page, state, queueCalls);

  console.log(
    JSON.stringify({
      top_layer: layer,
      fullscreen_owned: owned,
      presented: summarizePicture(shown),
    }),
  );
});
