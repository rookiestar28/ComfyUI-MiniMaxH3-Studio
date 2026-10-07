import { expect, test, type Page } from "@playwright/test";

import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { chooseAssetCommand } from "../helpers/nleBinMenus";

const openZeroClipMediaBin = async (page: Page) => {
  await routeGenericFixtureMedia(page);
  await page.goto("/nleWorkspace.html?media=1&zeroClips=1");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator('[data-h3-nle-region="asset-bin"]')).toBeVisible();
};

test("zero-clip media cards present released thumbnails and purge them on close", async ({
  page,
}) => {
  await openZeroClipMediaBin(page);

  await expect(page.locator("[data-h3-nle-thumbnail]")).toHaveCount(2);
  await expect
    .poll(
      () =>
        page.evaluate(
          () => window.nleWorkspaceHarness.snapshot().mediaOwnership,
        ),
      { message: "asset-scoped decoration authorities release after decode" },
    )
    .toMatchObject({ acquired: 2, released: 2, live: 0, surfaces: 2 });
  await expect(page.locator('[data-h3-nle-card-index="1"]')).toContainText(
    "Clip 01",
  );
  await expect(page.locator('[data-h3-nle-card-index="2"]')).toContainText(
    "Clip 02",
  );

  await page.locator('[data-h3-nle-action="close"]').click();
  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().mediaOwnership),
    )
    .toMatchObject({ live: 0, surfaces: 0, retainedBytes: 0 });
});

test("same-id fingerprint replacement clears copied pixels before reacquisition", async ({
  page,
}) => {
  await openZeroClipMediaBin(page);
  const canvas = page.locator(
    '[data-h3-nle-card-index="1"] [data-h3-nle-thumbnail]',
  );
  await expect(canvas).toHaveAttribute("width", "32");
  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().mediaOwnership),
    )
    .toMatchObject({ live: 0, surfaces: 2 });

  let releaseReplacement!: () => void;
  const replacementGate = new Promise<void>((resolve) => {
    releaseReplacement = resolve;
  });
  await page.route("**/nle-media/image*", async (route) => {
    await replacementGate;
    await route.fallback();
  });

  try {
    await page.evaluate(() =>
      window.nleWorkspaceHarness.replaceFirstMediaAssetFingerprint(),
    );
    await expect(canvas).toHaveAttribute("width", "0");
    await expect(canvas).toHaveAttribute("height", "0");
    await expect
      .poll(() =>
        page.evaluate(
          () => window.nleWorkspaceHarness.snapshot().mediaOwnership,
        ),
      )
      .toMatchObject({ live: 0, surfaces: 1 });
  } finally {
    releaseReplacement();
  }
  await expect(canvas).toHaveAttribute("width", "32");
  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().mediaOwnership),
    )
    .toMatchObject({ live: 0, surfaces: 2 });
});

test("card, range and Text controls each submit their canonical command once", async ({
  page,
}) => {
  await openZeroClipMediaBin(page);

  await page.locator('[data-h3-nle-control="asset.insert"]').first().click();
  // M25-63: Insert and Overwrite at the playhead live in the card's menu.
  const card = page.locator('[data-h3-nle-card-index="1"]');
  await chooseAssetCommand(page, card, "range.insert", "pointer");
  await chooseAssetCommand(page, card, "range.overwrite", "keyboard");
  await page.locator('[data-h3-nle-pane="text"]').click();
  await page.locator('[data-h3-nle-control="title.insert"]').click();

  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().intents.length),
    )
    .toBe(4);
  const kinds = await page.evaluate(() =>
    window.nleWorkspaceHarness
      .snapshot()
      .intents.map((intent) => intent.commands[0]?.kind),
  );
  expect(kinds).toEqual([
    "insert_asset_clip",
    "insert_range",
    "overwrite_range",
    "insert_title_clip",
  ]);
});

// B-M2564-10: every command disables the control that issued it while its transaction is pending,
// the card's trigger included, and a menu its command closed hands focus to the timeline grid (the
// dialog's focus keeper, `NleOverlay.tsx`). A keyboard user reaches the card's menu again once the
// trigger is usable. The helper's keyboard path focused the trigger without waiting, and
// `locator.focus()` -- unlike a click -- has no enabled check: inside the first command's busy
// window the focus did nothing and Shift+F10 reached the timeline grid, so no menu opened.
// `pendingMs` holds that window open, so the keyboard path lands in it every time.
test("B-M2564-10: the keyboard reaches a card's menu once the previous command has settled", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await page.goto("/nleWorkspace.html?media=1&zeroClips=1&pendingMs=400");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator('[data-h3-nle-region="asset-bin"]')).toBeVisible();
  const card = page.locator('[data-h3-nle-card-index="1"]');
  await chooseAssetCommand(page, card, "range.insert", "pointer");
  // The command is still pending: the card's trigger is not usable yet.
  await expect(card.locator(".h3-nle-media-primary")).toBeDisabled();
  await chooseAssetCommand(page, card, "range.overwrite", "keyboard");
  await expect
    .poll(() =>
      page.evaluate(() =>
        window.nleWorkspaceHarness
          .snapshot()
          .intents.map((intent) => intent.commands[0]?.kind),
      ),
    )
    .toEqual(["insert_range", "overwrite_range"]);
});

test("pointer drag commits one bin insert at the accepted timeline lane", async ({
  page,
}) => {
  await openZeroClipMediaBin(page);
  await expect(page.locator("[data-h3-nle-thumbnail]")).toHaveCount(2);
  await page.waitForTimeout(100);
  const card = page.locator(
    '[data-h3-nle-card-index="1"] .h3-nle-media-primary',
  );
  const tracks = page.locator(".h3-nle-tracks");
  const source = (await card.boundingBox())!;
  const target = (await tracks.boundingBox())!;

  await page.mouse.move(
    source.x + source.width / 2,
    source.y + source.height / 2,
  );
  await page.mouse.down();
  expect(await card.evaluate((element) => element.hasPointerCapture(1))).toBe(
    true,
  );
  const drop = { clientX: target.x + 260, clientY: target.y + 24 };
  await card.dispatchEvent("pointermove", {
    ...drop,
    pointerId: 1,
    isPrimary: true,
    button: 0,
    buttons: 1,
  });
  await expect(
    page.locator('[data-h3-nle-canvas="timeline_decoration"]'),
  ).toHaveAttribute("data-h3-nle-ghost", "admitted");
  await card.dispatchEvent("pointerup", {
    ...drop,
    pointerId: 1,
    isPrimary: true,
    button: 0,
    buttons: 0,
  });
  await page.mouse.up();

  await expect
    .poll(() =>
      page.evaluate(() => window.nleWorkspaceHarness.snapshot().intents.length),
    )
    .toBe(1);
  const commands = await page.evaluate(
    () => window.nleWorkspaceHarness.snapshot().intents[0]?.commands,
  );
  expect(commands).toHaveLength(1);
  expect(commands?.[0]?.kind).toBe("insert_asset_clip");
});

// B-M2564-01: a command in flight disables the menu's items. The focused item drops focus, the
// dialog's focus keeper parks it on the heading, and the bin menu's outside-focus dismissal then
// closes a menu the user never left. The menu must hold focus itself while no item can take it
// and hand it to its first item when the command settles. `pendingMs` holds the busy
// window open; the command starts from script, so neither the pointer nor focus leaves the menu.
test("an open card menu survives a command that starts elsewhere and keeps focus", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await page.goto("/nleWorkspace.html?media=1&zeroClips=1&pendingMs=1500");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator('[data-h3-nle-region="asset-bin"]')).toBeVisible();
  const card = page.locator('[data-h3-nle-card-index="1"]');
  const assetId = await card.getAttribute("data-h3-nle-asset");
  await card.locator(".h3-nle-media-primary").click({ button: "right" });
  const menu = page.locator(`[data-h3-nle-asset-menu="${assetId}"]`);
  const first = menu.locator('[role="menuitem"]').first();
  await expect(first).toBeFocused();

  await page.evaluate(() =>
    document
      .querySelector<HTMLButtonElement>(
        '[data-h3-nle-card-index="2"] [data-h3-nle-control="asset.insert"]',
      )!
      .click(),
  );
  await expect(first).toBeDisabled();
  await expect(menu).toBeVisible();
  await expect(menu).toBeFocused();

  await expect(first).toBeEnabled({ timeout: 10_000 });
  await expect(menu).toBeVisible();
  await expect(first).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(menu).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(() =>
        window.nleWorkspaceHarness
          .snapshot()
          .intents.map((intent) => intent.commands[0]?.kind),
      ),
    )
    .toEqual(["insert_asset_clip", "insert_range"]);
});
