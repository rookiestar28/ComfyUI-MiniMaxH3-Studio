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
  openH3AppModeTab,
  repositoryRoot,
  supportedHostQueueCounts,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "../../host/environment";

/**
 * M25-41 supplied-host observation: the sidebar Production tab's whole-video sequence actions are
 * icon tiles at the 704 px panel floor (M25-43: equal tiles on a shared grid), their descriptions appear on hover and keyboard
 * focus inside the panel, and a real planning action still dispatches. The only execution is the
 * model-free Context bootstrap; no generation is queued.
 */

const CONTAINER_ID = "h3-context-m25-41-toolbar";
const PRODUCTION_ROUTE = "/h3-context/v1/production/action";
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

test("M25-41 planning icon toolbar on the supplied host at the 704 px floor", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.H3_CONTEXT_M25_41_HOST !== "1",
    "explicit supplied-host toolbar observation required",
  );
  test.setTimeout(10 * 60_000);
  if (!hostUrl || !candidateBundle || candidateBackendMode !== "exact")
    throw new Error(
      "an exact installed candidate and supplied host are required",
    );
  const evidenceValue = process.env.H3_CONTEXT_M25_41_EVIDENCE;
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
    // The sidebar reaches owned routes through the host's `/api` prefix; an exact pathname compare
    // never sees its create response, so cleanup would not learn the workspace it must release.
    if (
      normalizeM2508HostApiPath(new URL(response.url()).pathname) !==
      PRODUCTION_ROUTE
    )
      return;
    const action = String(response.request().postDataJSON()?.action);
    productionActions.push(`${action}:${response.status()}`);
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
  // The helper renders the registered tab into a 44rem (704 px) host panel.
  const shell = await openH3AppModeTab(page, CONTAINER_ID);
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
    `Synthetic moving color landmarks. M25-41 ${Date.now()}.`;
  if (
    !Object.values(bootstrap).every((node: any) =>
      CONTEXT_ONLY.has(node.class_type),
    )
  )
    throw new Error(
      "context bootstrap includes an unauthorized execution node",
    );

  const observation: Record<string, unknown> = {
    schema: "M2541HostToolbarObservationV1",
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

    await shell.locator('[data-page-id="production"]').click();
    await expect.poll(() => productionHandle, { timeout: 30_000 }).toBeTruthy();
    const section = shell.locator("[data-h3-nle-planning-status]");
    await expect(section).toBeVisible({ timeout: 30_000 });
    const prepare = section.locator(
      '[data-h3-nle-control="planning.prepare_context"]',
    );
    await expect(prepare).toBeEnabled({ timeout: 60_000 });
    await section.scrollIntoViewIfNeeded();

    const geometry = await section.evaluate((root) => {
      const panel = root.closest(".h3c")!.getBoundingClientRect();
      const buttons = [...root.querySelectorAll("button.h3-icon-button")].map(
        (button) => {
          const box = button.getBoundingClientRect();
          return {
            control: button.getAttribute("data-h3-nle-control"),
            x: box.x,
            y: box.y,
            width: box.width,
            height: box.height,
            text: (button.textContent ?? "").trim(),
            label: button.getAttribute("aria-label"),
          };
        },
      );
      return {
        panelWidth: panel.width,
        panelLeft: panel.left,
        panelRight: panel.right,
        overflow:
          root.closest(".h3c")!.scrollWidth - root.closest(".h3c")!.clientWidth,
        buttons,
      };
    });
    observation.geometry = geometry;
    expect(geometry.overflow).toBeLessThanOrEqual(0);
    expect(geometry.buttons.length).toBeGreaterThanOrEqual(6);
    // M25-43: the actions are equal tiles on a shared column grid that fills the row.
    for (const button of geometry.buttons) {
      expect(
        Math.abs(button.width - geometry.buttons[0]!.width),
        `${button.control} shared tile width`,
      ).toBeLessThanOrEqual(1);
      expect(
        button.width,
        `${button.control} target width`,
      ).toBeGreaterThanOrEqual(44);
      expect(button.height, `${button.control} height`).toBeCloseTo(44, 0);
      expect(button.text, `${button.control} text`).toBe("");
      expect(
        button.label?.length ?? 0,
        `${button.control} name`,
      ).toBeGreaterThan(0);
    }
    const rows = [...geometry.buttons].sort((a, b) => a.y - b.y || a.x - b.x);
    for (let index = 1; index < rows.length; index += 1) {
      const previous = rows[index - 1]!;
      const current = rows[index]!;
      if (Math.abs(current.y - previous.y) < 1)
        expect(
          current.x - (previous.x + previous.width),
          `${current.control} gap`,
        ).toBeGreaterThanOrEqual(7.5);
    }
    await section.screenshot({ path: resolve(evidence, "host-704-en.png") });

    const tip = section.locator("[role='tooltip']:not([hidden])");
    const tipInside = () =>
      tip.evaluate((node) => {
        const box = node.getBoundingClientRect();
        const panel = node.closest(".h3c")!.getBoundingClientRect();
        const row = node
          .closest(".h3-icon-group")!
          .querySelector(".h3-icon-row")!
          .getBoundingClientRect();
        let clip = { top: 0, bottom: innerHeight };
        for (
          let parent = node.parentElement;
          parent;
          parent = parent.parentElement
        )
          if (getComputedStyle(parent).overflowY !== "visible") {
            clip = parent.getBoundingClientRect();
            break;
          }
        return {
          text: node.textContent ?? "",
          placement: node.getAttribute("data-placement"),
          inside:
            box.width > 0 &&
            box.left >= panel.left - 0.5 &&
            box.right <= panel.right + 0.5 &&
            box.top >= Math.max(clip.top, 0) - 0.5 &&
            box.bottom <= Math.min(clip.bottom, innerHeight) + 0.5 &&
            (box.bottom <= row.top + 0.5 || box.top >= row.bottom - 0.5),
        };
      });
    const positions = () =>
      section.locator("[data-h3-nle-control]").evaluateAll((nodes) =>
        nodes.map((node) => {
          const box = node.getBoundingClientRect();
          return `${node.getAttribute("data-h3-nle-control")}@${Math.round(box.x)},${Math.round(box.y)}`;
        }),
      );
    await prepare.scrollIntoViewIfNeeded();
    const resting = await positions();
    await prepare.hover();
    await expect(tip).toHaveCount(1);
    const hovered = await tipInside();
    expect(hovered.inside).toBe(true);
    // The description overlays the panel: no action moves while it is visible.
    const hoveredPositions = await positions();
    expect(hoveredPositions).toEqual(resting);
    observation.actionsMovedByDescription = hoveredPositions.filter(
      (position, index) => position !== resting[index],
    ).length;
    expect(hovered.text.length).toBeGreaterThan(0);
    await section.screenshot({
      path: resolve(evidence, "host-704-en-hover.png"),
    });
    const unavailable = section.locator(
      '[data-h3-nle-control="planning.propose"]',
    );
    await expect(unavailable).toBeDisabled();
    await unavailable.locator("xpath=..").hover();
    await expect(tip).toHaveText(/Production segments/);
    await shell.locator("[data-page-id='production']").hover();
    await expect(tip).toHaveCount(0);
    await prepare.focus();
    await expect(tip).toHaveCount(1);
    const focused = await tipInside();
    expect(focused.inside).toBe(true);
    await section.screenshot({
      path: resolve(evidence, "host-704-en-focus.png"),
    });
    await page.keyboard.press("Escape");
    await expect(tip).toHaveCount(0);
    observation.descriptions = { hovered, focused };

    const postsBeforeAction = promptPosts;
    await prepare.click();
    await expect(section).toHaveAttribute(
      "data-h3-nle-planning-status",
      "prepared",
      { timeout: 60_000 },
    );
    expect(promptPosts).toBe(postsBeforeAction);
    expect(await supportedHostQueueCounts(page)).toEqual({
      running: 0,
      pending: 0,
    });
    observation.action = {
      control: "planning.prepare_context",
      status: "prepared",
      promptPostsDuringAction: promptPosts - postsBeforeAction,
    };
  } finally {
    if (productionHandle !== undefined) {
      const read = await post(
        page,
        PRODUCTION_ROUTE,
        encodeProductionAction("m25_41.cleanup.read", "read_projection", {
          workspaceHandle: productionHandle,
        }),
      );
      const released =
        read.status === 200
          ? await post(
              page,
              PRODUCTION_ROUTE,
              encodeProductionAction(
                "m25_41.cleanup.release",
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
