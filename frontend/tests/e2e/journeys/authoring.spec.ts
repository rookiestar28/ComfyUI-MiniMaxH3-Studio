import { expect, test } from "../fixtures/h3Page";

// M20-03, re-aimed by M25-44 (one NLE): the Clip editor function no longer mounts an editing
// surface. It shows the full-editor launcher and a read-only project summary that owns the
// Authoring workspace lifecycle the compact editor used to offer -- start from the current Context,
// refresh, and a labelled two-step release. Editing is covered on the full editor
// (`nleReferenceShell`, `nleWorkspace`, `nleCommandMatrix`). The harness simulates the bounded
// backend; every assertion is a state transition the user can observe, never DOM existence alone.

const SUMMARY = { name: "Clip editor project", exact: true } as const;
const LEGACY_EDITOR = { name: "Reference & timeline authoring" } as const;

test.beforeEach(async ({ page }) => {
  await page.goto("/?mode=production");
  await page
    .getByRole("navigation", { name: "H3 Context pages" })
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor" }).click();
});

async function createWorkspace(page: import("@playwright/test").Page) {
  const summary = page.getByRole("region", SUMMARY);
  await summary
    .getByRole("button", { name: "Start authoring from this context" })
    .click();
  await expect(summary.getByRole("status")).toHaveText(
    "Authoring workspace ready. Open the full editor to load its timeline.",
  );
  return summary;
}

// B-M2544-02: the lifecycle actions follow the owner's sidebar action-row rule. Each is an icon tile
// (no visible text; the full label is its accessible name) on the shared five-column grid that spans
// the summary edge to edge, all in one row starting at the left, with its description on hover.
async function expectActionTiles(
  summary: import("@playwright/test").Locator,
  labels: readonly string[],
) {
  const row = await summary.locator(".h3-icon-row").evaluate((node) => {
    const rect = node.getBoundingClientRect();
    const section = node.closest("section")!;
    const style = getComputedStyle(section);
    const inner = section.getBoundingClientRect();
    return {
      left: rect.left,
      width: rect.width,
      right: rect.right,
      contentLeft:
        inner.left +
        parseFloat(style.borderLeftWidth) +
        parseFloat(style.paddingLeft),
      contentRight:
        inner.right -
        parseFloat(style.borderRightWidth) -
        parseFloat(style.paddingRight),
    };
  });
  expect(Math.abs(row.left - row.contentLeft)).toBeLessThanOrEqual(1);
  expect(Math.abs(row.right - row.contentRight)).toBeLessThanOrEqual(1);
  const tiles = await summary.getByRole("button").evaluateAll((nodes) =>
    nodes.map((node) => {
      const rect = node.getBoundingClientRect();
      return {
        label: node.getAttribute("aria-label"),
        text: (node.textContent ?? "").trim(),
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
      };
    }),
  );
  expect(tiles.map((tile) => tile.label)).toEqual(labels);
  const column = (row.width - 4 * 8) / 5;
  tiles.forEach((tile, index) => {
    expect(tile.text).toBe("");
    expect(tile.height).toBe(44);
    expect(Math.round(tile.y)).toBe(Math.round(tiles[0]!.y));
    expect(Math.abs(tile.width - column)).toBeLessThanOrEqual(1);
    expect(
      Math.abs(tile.x - (row.left + index * (column + 8))),
    ).toBeLessThanOrEqual(1);
  });
}

async function actionCount(page: import("@playwright/test").Page) {
  return Number(
    (await page.locator("#authoring-action-count").textContent()) ?? "0",
  );
}

test("creates the workspace and renders the accepted backend state", async ({
  page,
}) => {
  const summary = page.getByRole("region", SUMMARY);
  await expect(summary.getByRole("status")).toHaveText(
    "No authoring workspace yet.",
  );
  await expect(page.locator("#authoring-action-count")).toHaveText("0");
  // No workspace offers only the start: nothing to refresh or release yet.
  await expect(summary.getByRole("button")).toHaveCount(1);
  await createWorkspace(page);
  await expect(page.locator("#authoring-last-action")).toHaveText(
    "create_authoring_workspace",
  );
  await expect(page.locator("#authoring-action-count")).toHaveText("1");
  await expect(
    summary.getByRole("button", { name: "Start authoring from this context" }),
  ).toHaveCount(0);
  await expect(
    summary.getByRole("button", { name: "Refresh workspace" }),
  ).toBeEnabled();
  await expect(
    summary.getByRole("button", { name: "Release workspace" }),
  ).toBeEnabled();
  await expectActionTiles(summary, ["Refresh workspace", "Release workspace"]);
  await summary.getByRole("button", { name: "Release workspace" }).hover();
  await expect(summary.getByRole("tooltip")).toHaveText(
    "Release this workspace. The next step asks you to confirm.",
  );
  // One editing surface: the tab mounts no compact editor and no timeline edit control.
  await expect(page.getByRole("region", LEGACY_EDITOR)).toHaveCount(0);
  await expect(page.locator("section.h3a")).toHaveCount(0);
  await expect(page.locator("[data-h3-nle-control]")).toHaveCount(0);
});

test("refresh reads the accepted workspace once and changes nothing else", async ({
  page,
}) => {
  const summary = await createWorkspace(page);
  const before = await actionCount(page);
  await summary.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(page.locator("#authoring-last-action")).toHaveText(
    "read_projection",
  );
  await expect(page.locator("#authoring-action-count")).toHaveText(
    String(before + 1),
  );
  await expect(summary.getByRole("status")).toHaveText(
    "Authoring workspace ready. Open the full editor to load its timeline.",
  );
  await expect(
    summary.getByRole("button", { name: "Refresh workspace" }),
  ).toBeEnabled();
  // Waiting adds no hidden read.
  await page.waitForTimeout(500);
  await expect(page.locator("#authoring-action-count")).toHaveText(
    String(before + 1),
  );
});

test("function tabs manually activate, persist for page round trips, and do no hidden work", async ({
  page,
}) => {
  await page.reload();
  const navigation = page.getByRole("navigation", { name: "H3 Context pages" });
  await navigation.getByRole("button", { name: "Production" }).click();
  const pageIds = await navigation
    .locator("button[data-page-id]")
    .evaluateAll((buttons) =>
      buttons.map((button) => button.getAttribute("data-page-id")),
    );
  expect(pageIds).toEqual(["context", "production", "settings"]);
  const tablist = page.getByRole("tablist", { name: "Production functions" });
  await expect(tablist).toHaveAttribute("data-h3-director-function-tabs", "v1");
  const production = page.getByRole("tab", { name: "Production" });
  const clip = page.getByRole("tab", { name: "Clip editor" });
  await expect(production).toHaveAttribute("aria-selected", "true");
  await production.focus();
  const actionsBefore = await page
    .locator("#authoring-action-count")
    .textContent();
  await page.keyboard.press("ArrowRight");
  await expect(clip).toBeFocused();
  await expect(production).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("region", SUMMARY)).toHaveCount(0);
  await page.keyboard.press("Enter");
  await expect(clip).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("region", SUMMARY)).toBeVisible();
  await expect(page.locator("[data-h3-director-panel]")).toHaveCount(1);
  await expect(page.locator("#authoring-action-count")).toHaveText(
    actionsBefore ?? "0",
  );
  await expect(page.locator("#authoring-preview-count")).toHaveText("0");

  await navigation.getByRole("button", { name: "Context" }).click();
  await expect(clip).toHaveCount(0);
  await navigation.getByRole("button", { name: "Production" }).click();
  await expect(page.getByRole("tab", { name: "Clip editor" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("release requires labelled confirmation and lands in released", async ({
  page,
}) => {
  const summary = await createWorkspace(page);
  const before = await actionCount(page);
  await summary.getByRole("button", { name: "Release workspace" }).click();
  await expect(
    summary.getByRole("button", { name: "Confirm release" }),
  ).toBeVisible();
  await expectActionTiles(summary, [
    "Refresh workspace",
    "Confirm release",
    "Keep workspace",
  ]);
  await summary.getByRole("button", { name: "Keep workspace" }).click();
  await expect(
    summary.getByRole("button", { name: "Confirm release" }),
  ).toHaveCount(0);
  await expect(page.locator("#authoring-action-count")).toHaveText(
    String(before),
  );
  await summary.getByRole("button", { name: "Release workspace" }).click();
  await summary.getByRole("button", { name: "Confirm release" }).click();
  await expect(summary.getByRole("status")).toHaveText(
    "The authoring workspace was released.",
  );
  await expect(page.locator("#authoring-last-action")).toHaveText(
    "release_workspace",
  );
  await expect(page.locator("#authoring-action-count")).toHaveText(
    String(before + 1),
  );
  // A released workspace offers a fresh start and nothing that would act on the old one.
  await expect(
    summary.getByRole("button", { name: "Start authoring from this context" }),
  ).toBeEnabled();
  await expect(
    summary.getByRole("button", { name: "Refresh workspace" }),
  ).toHaveCount(0);
});
