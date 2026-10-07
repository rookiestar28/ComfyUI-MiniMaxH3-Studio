import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import {
  decodeProductionWorkbenchProjection,
  encodeProductionAction,
} from "../../../../src/contracts/productionWorkbenchCodec";
import {
  ensureM2508CapturedWorkflowAuthority,
  materializeM2508VisiblePromptInCapturedWorkflow,
} from "../../host/m25_08Bootstrap";
import { normalizeM2508HostApiPath } from "../../host/m25_08RequestClassification";
import { ownedPath, post } from "../../host/productionRuntimeRow";
import { expect, test } from "../../host/fixture";
import {
  assertCandidateBundleInjection,
  candidateBackendMode,
  candidateBundle,
  candidateInjectionCount,
  hostUrl,
  repositoryRoot,
  setSupportedH3Language,
  supportedHostQueueCounts,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "../../host/environment";

/**
 * M25-43 supplied-host observation in the host's own sidebar, not a helper panel. The H3 tab opens
 * at the 704 px floor; the planning actions tile one shared five-column grid to the row's right
 * edge; the pointer shows `not-allowed` over an unavailable action and a hand over an available
 * one; dragging the real splitter gutter wider makes the content wrapper, the H3 root and the
 * planning rows follow the panel, and dragging it narrower stops at the floor. The only execution
 * is the model-free Context bootstrap; no generation is queued.
 */

const PRODUCTION_ROUTE = "/h3-context/v1/production/action";
const FLOOR = 704;
const MAIN = [
  "planning.prepare_context",
  "planning.admit_canonical",
  "planning.review_storyboard",
  "planning.propose",
  "planning.approve_import",
];
const CONTEXT_ONLY = new Set(
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

test.use({ viewport: { width: 1920, height: 1080 } });

type Layout = {
  panel: number;
  panelClient: number;
  content: number;
  contentClient: number;
  contentInlineWidth: string;
  root: number;
  section: number;
  gutter: { x: number; y: number; side: "right" | "left" } | null;
  groups: {
    label: string | null;
    row: { left: number; right: number };
    rowOverflow: number;
    chip: { left: number; right: number } | null;
    buttons: {
      control: string;
      x: number;
      y: number;
      width: number;
      height: number;
    }[];
  }[];
};

test("M25-43 real sidebar follows its gutter and tiles its planning actions", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.H3_CONTEXT_M25_43_HOST !== "1",
    "explicit supplied-host sidebar resize observation required",
  );
  test.setTimeout(10 * 60_000);
  if (!hostUrl || !candidateBundle || candidateBackendMode !== "exact")
    throw new Error(
      "an exact installed candidate and supplied host are required",
    );
  const evidenceValue = process.env.H3_CONTEXT_M25_43_EVIDENCE;
  if (!evidenceValue)
    throw new Error("an owned evidence directory is required");
  const evidence = ownedPath(evidenceValue);
  await mkdir(evidence, { recursive: true });

  let promptPosts = 0;
  let productionHandle: string | undefined;
  const productionActions: string[] = [];
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname.endsWith("/prompt")
    )
      promptPosts += 1;
  });
  page.on("response", async (response) => {
    if (
      normalizeM2508HostApiPath(new URL(response.url()).pathname) !==
      PRODUCTION_ROUTE
    )
      return;
    const action = String(response.request().postDataJSON()?.action);
    // A refusal's error code is a closed product vocabulary; record it so a failure names it.
    const refusal =
      response.status() >= 400
        ? `:${String((await response.json().catch(() => ({})))?.error)}`
        : "";
    productionActions.push(`${action}:${response.status()}${refusal}`);
    if (action === "create_workspace_from_context" && response.status() === 201)
      productionHandle = decodeProductionWorkbenchProjection(
        await response.json(),
      ).workspaceHandle;
  });

  expect(await supportedHostQueueCounts(page)).toEqual({
    running: 0,
    pending: 0,
  });
  const injections = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injections);
  const dialog = page.locator(
    '[role="dialog"][aria-labelledby="global-workflow-template-selector"]',
  );
  if (await dialog.isVisible()) {
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  }
  await setSupportedH3Language(page, "en");

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
    `Synthetic moving color landmarks. M25-43 ${Date.now()}.`;
  if (
    !Object.values(bootstrap).every((node: any) =>
      CONTEXT_ONLY.has(node.class_type),
    )
  )
    throw new Error(
      "context bootstrap includes an unauthorized execution node",
    );

  const observation: Record<string, unknown> = {
    schema: "M2543HostSidebarResizeObservationV1",
  };
  const measure = () =>
    page.evaluate(() => {
      const section = document.querySelector<HTMLElement>(
        "[data-h3-nle-planning-status]",
      )!;
      const root = section.closest<HTMLElement>("section.h3c")!;
      const content = root.closest<HTMLElement>(".sidebar-content-container")!;
      const panel = root.closest<HTMLElement>(
        ".side-bar-panel, .p-splitterpanel",
      )!;
      const next = panel.nextElementSibling;
      const previous = panel.previousElementSibling;
      const gutterElement = next?.classList.contains("p-splitter-gutter")
        ? next
        : previous?.classList.contains("p-splitter-gutter")
          ? previous
          : null;
      const gutterBox = gutterElement?.getBoundingClientRect();
      const box = (node: Element) => {
        const value = node.getBoundingClientRect();
        return { left: value.left, right: value.right };
      };
      return {
        panel: panel.getBoundingClientRect().width,
        panelClient: panel.clientWidth,
        content: content.getBoundingClientRect().width,
        contentClient: content.clientWidth,
        contentInlineWidth: content.style.width,
        root: root.getBoundingClientRect().width,
        section: section.getBoundingClientRect().width,
        gutter: gutterBox
          ? {
              x: gutterBox.x + gutterBox.width / 2,
              y: gutterBox.y + gutterBox.height / 2,
              side: gutterElement === next ? "right" : "left",
            }
          : null,
        groups: [...section.querySelectorAll(".h3-icon-group")].map((group) => {
          const row = group.querySelector(".h3-icon-row")!;
          const chip = group.querySelector(".h3-nle-chip");
          return {
            label: group.getAttribute("aria-label"),
            row: box(row),
            rowOverflow: row.scrollWidth - row.clientWidth,
            chip: chip ? box(chip) : null,
            buttons: [...group.querySelectorAll("button.h3-icon-button")].map(
              (button) => {
                const value = button.getBoundingClientRect();
                return {
                  control: button.getAttribute("data-h3-nle-control")!,
                  x: value.x,
                  y: value.y,
                  width: value.width,
                  height: value.height,
                };
              },
            ),
          };
        }),
      };
    }) as Promise<Layout>;

  const expectTiles = (layout: Layout, where: string) => {
    const main = layout.groups.find((group) =>
      group.buttons.some((button) => button.control === MAIN[0]),
    )!;
    expect(main, `${where} planning actions`).toBeDefined();
    const columns = [...main.buttons].sort((a, b) => a.x - b.x);
    expect(
      columns.map((button) => button.control),
      where,
    ).toEqual(MAIN);
    expect(new Set(columns.map((button) => Math.round(button.y))).size).toBe(1);
    expect(
      Math.abs(columns[0]!.x - main.row.left),
      `${where} left edge`,
    ).toBeLessThanOrEqual(1);
    const last = columns[4]!;
    expect(
      Math.abs(last.x + last.width - main.row.right),
      `${where} right edge`,
    ).toBeLessThanOrEqual(1);
    for (let index = 1; index < 5; index += 1)
      expect(
        Math.abs(
          columns[index]!.x -
            (columns[index - 1]!.x + columns[index - 1]!.width) -
            8,
        ),
        `${where} gap ${index}`,
      ).toBeLessThanOrEqual(1);
    for (const group of layout.groups) {
      expect(
        group.rowOverflow,
        `${where} ${group.label} overflow`,
      ).toBeLessThanOrEqual(0);
      const ordered = [...group.buttons].sort((a, b) => a.y - b.y || a.x - b.x);
      ordered.forEach((button, index) => {
        expect(
          Math.abs(button.width - columns[0]!.width),
          `${where} ${button.control} width`,
        ).toBeLessThanOrEqual(1);
        expect(button.height, `${where} ${button.control} height`).toBeCloseTo(
          44,
          0,
        );
        expect(
          Math.abs(button.x - columns[index]!.x),
          `${where} ${button.control} column`,
        ).toBeLessThanOrEqual(1);
      });
      if (group.chip !== null) {
        expect(
          Math.abs(group.chip.left - columns[ordered.length]!.x),
          `${where} chip start`,
        ).toBeLessThanOrEqual(1);
        expect(
          Math.abs(group.chip.right - main.row.right),
          `${where} chip end`,
        ).toBeLessThanOrEqual(1);
      }
    }
  };

  const expectFollowing = (layout: Layout, where: string) => {
    expect(
      layout.contentInlineWidth,
      `${where} content inline width`,
    ).not.toMatch(/px$/);
    expect(
      Math.abs(layout.content - layout.panelClient),
      `${where} content`,
    ).toBeLessThanOrEqual(1);
    expect(
      Math.abs(layout.root - layout.contentClient),
      `${where} root`,
    ).toBeLessThanOrEqual(1);
  };

  const dragGutter = async (layout: Layout, delta: number) => {
    const gutter = layout.gutter!;
    const signed = gutter.side === "right" ? delta : -delta;
    await page.mouse.move(gutter.x, gutter.y);
    await page.mouse.down();
    await page.mouse.move(gutter.x + signed, gutter.y, { steps: 15 });
    await page.mouse.up();
    await page.mouse.move(0, 0);
  };

  const settledLayout = async (panelWidth: (width: number) => boolean) => {
    await expect
      .poll(async () => panelWidth((await measure()).panel), {
        timeout: 10_000,
      })
      .toBe(true);
    await page.evaluate(
      () =>
        new Promise((done) =>
          requestAnimationFrame(() => requestAnimationFrame(done)),
        ),
    );
    return measure();
  };

  try {
    await page.evaluate(materializeM2508VisiblePromptInCapturedWorkflow, {
      ...bootstrap,
      "7": sourceFixture.prompt["7"],
    });
    await page.evaluate(ensureM2508CapturedWorkflowAuthority);
    await waitForHostGraphSettled(page);
    const promptId = await page.evaluate(async (prompt) => {
      const runtime = window as unknown as {
        comfyAPI: { api: { api: any } };
      };
      const response = await fetch("/prompt", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          prompt,
          client_id: runtime.comfyAPI.api.api.clientId,
        }),
      });
      if (!response.ok)
        throw new Error(`context bootstrap refused: ${response.status}`);
      return String((await response.json()).prompt_id);
    }, bootstrap);
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            new URL(`/history/${encodeURIComponent(promptId)}`, hostUrl).href,
          );
          return (
            (await response.json())[promptId]?.outputs?.["6"]
              ?.sidebar_workspace?.[0] !== undefined
          );
        },
        { timeout: 60_000 },
      )
      .toBe(true);
    await expect
      .poll(() => supportedHostQueueCounts(page))
      .toEqual({ running: 0, pending: 0 });
    await waitForHostGraphSettled(page);

    // The host's own activity-bar button opens the tab into the host's own splitter panel.
    await page.locator("button:has(.pi-sparkles)").first().click();
    const root = page.locator(".side-bar-panel section.h3c").first();
    await expect(root).toBeVisible({ timeout: 30_000 });
    await root.locator('[data-page-id="production"]').click();
    try {
      await expect
        .poll(() => productionHandle, { timeout: 30_000 })
        .toBeTruthy();
    } catch (error) {
      // Production refusals carry no body; the page's own status line is what names the cause.
      observation.productionWithoutHandle = await root.evaluate((node) =>
        [...node.querySelectorAll('[role="status"], [role="alert"]')]
          .map((status) => (status.textContent ?? "").trim())
          .filter((text) => text.length > 0),
      );
      await root.screenshot({
        path: resolve(evidence, "host-production-without-handle.png"),
      });
      throw error;
    }
    const section = root.locator("[data-h3-nle-planning-status]");
    await expect(section).toBeVisible({ timeout: 30_000 });
    const prepare = section.locator(
      '[data-h3-nle-control="planning.prepare_context"]',
    );
    await expect(prepare).toBeEnabled({ timeout: 60_000 });
    await section.scrollIntoViewIfNeeded();

    const atFloor = await settledLayout(
      (width) => Math.abs(width - FLOOR) <= 1,
    );
    observation.atFloor = atFloor;
    expect(
      atFloor.gutter,
      "the host splitter gutter beside the H3 panel",
    ).not.toBeNull();
    expectFollowing(atFloor, "floor");
    expectTiles(atFloor, "floor");
    await section.screenshot({ path: resolve(evidence, "host-floor-en.png") });

    // The cursor a user sees is the one on the element the pointer hits.
    const cursorAt = async (control: string) => {
      const button = section.locator(`[data-h3-nle-control="${control}"]`);
      await button.scrollIntoViewIfNeeded();
      const box = (await button.boundingBox())!;
      const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      await page.mouse.move(point.x, point.y, { steps: 4 });
      return button.evaluate((node, at) => {
        const hit = document.elementFromPoint(at.x, at.y)!;
        return {
          disabled: (node as HTMLButtonElement).disabled,
          cursor: getComputedStyle(hit).cursor,
          hitInSlot: node.parentElement!.contains(hit),
        };
      }, point);
    };
    const unavailable = await cursorAt("planning.propose");
    const available = await cursorAt("planning.prepare_context");
    await page.mouse.move(0, 0);
    observation.cursors = { unavailable, available };
    expect(unavailable).toEqual({
      disabled: true,
      cursor: "not-allowed",
      hitInSlot: true,
    });
    expect(available).toEqual({
      disabled: false,
      cursor: "pointer",
      hitInSlot: true,
    });

    await dragGutter(atFloor, 300);
    const widened = await settledLayout((width) => width > FLOOR + 250);
    observation.widened = widened;
    expectFollowing(widened, "widened");
    expectTiles(widened, "widened");
    expect(
      widened.section - atFloor.section,
      "the planning section grew",
    ).toBeGreaterThan(250);
    await section.screenshot({
      path: resolve(evidence, "host-widened-en.png"),
    });

    await dragGutter(widened, -700);
    const narrowed = await settledLayout(
      (width) => width < widened.panel - 250,
    );
    observation.narrowed = narrowed;
    expect(
      Math.abs(narrowed.panel - FLOOR),
      "the panel holds the floor",
    ).toBeLessThanOrEqual(1);
    expectFollowing(narrowed, "narrowed");
    expectTiles(narrowed, "narrowed");
  } finally {
    if (productionHandle !== undefined) {
      const read = await post(
        page,
        PRODUCTION_ROUTE,
        encodeProductionAction("m25_43.cleanup.read", "read_projection", {
          workspaceHandle: productionHandle,
        }),
      );
      const released =
        read.status === 200
          ? await post(
              page,
              PRODUCTION_ROUTE,
              encodeProductionAction(
                "m25_43.cleanup.release",
                "release_workspace",
                {
                  projection: decodeProductionWorkbenchProjection(read.body),
                },
              ),
            )
          : read;
      observation.cleanup = { read: read.status, release: released.status };
    }
    observation.promptPosts = promptPosts;
    observation.productionActions = productionActions;
    await writeFile(
      resolve(evidence, "host-observation.json"),
      JSON.stringify(observation, null, 2),
      "utf8",
    );
  }
});
