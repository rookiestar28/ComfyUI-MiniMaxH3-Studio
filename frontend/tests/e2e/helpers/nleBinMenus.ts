import { expect, type Locator, type Page } from "@playwright/test";

/**
 * M25-63: the media bin's sort and filter menu and a card's context menu, driven the way a user
 * reaches them. `via: "keyboard"` never clicks: it focuses the trigger, opens with ArrowDown or
 * Shift+F10, walks the items with the arrow keys and activates with Enter.
 */
export type Via = "pointer" | "keyboard";

const SORT_FILTER = '[data-h3-nle-control="media.sort_filter"]';

export async function chooseMediaOption(
  scope: Page | Locator,
  group: "media.sort" | "media.filter",
  value: string,
  via: Via = "pointer",
): Promise<void> {
  const trigger = scope.locator(SORT_FILTER);
  const page = "page" in scope ? scope.page() : scope;
  const menu = page.locator('[role="menu"]').filter({
    has: page.locator(`[data-h3-nle-control="${group}"]`),
  });
  const item = menu.locator(
    `[data-h3-nle-control="${group}"] [data-h3-nle-value="${value}"]`,
  );
  if (via === "pointer") {
    await trigger.click();
    await expect(menu).toBeVisible();
    await item.click();
  } else {
    await trigger.focus();
    await page.keyboard.press("ArrowDown");
    await expect(menu).toBeVisible();
    const items = menu.locator('[role="menuitemradio"]');
    const values = await items.evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-h3-nle-value")),
    );
    const groups = await items.evaluateAll((nodes) =>
      nodes.map((node) =>
        node
          .closest("[data-h3-nle-control]")
          ?.getAttribute("data-h3-nle-control"),
      ),
    );
    const target = values.findIndex(
      (candidate, index) => candidate === value && groups[index] === group,
    );
    expect(
      target,
      `${group}=${value} in ${values.join(",")}`,
    ).toBeGreaterThanOrEqual(0);
    for (let step = 0; step < target; step += 1)
      await page.keyboard.press("ArrowDown");
    await expect(item).toBeFocused();
    await page.keyboard.press("Enter");
  }
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();
}

/** The open asset menu for `card` (a `[data-h3-nle-asset]` item). */
export async function openAssetMenu(
  page: Page,
  card: Locator,
  via: Via = "pointer",
): Promise<Locator> {
  // Right-click and Shift+F10 open the menu from the card itself; the "…" trigger is coarse-only.
  const trigger = card.locator(".h3-nle-media-primary");
  if (via === "pointer") await trigger.click({ button: "right" });
  else {
    // IMPORTANT (M25-64 B-M2564-10): a command disables the card's trigger until it settles, and
    // `locator.focus()` has no enabled check (a click has): focusing it inside that window does
    // nothing and Shift+F10 reaches the timeline grid. Reach it as a keyboard user can -- once it
    // is usable -- and prove the focus landed before opening the menu.
    await expect(trigger).toBeEnabled();
    await trigger.focus();
    await expect(trigger).toBeFocused();
    await page.keyboard.press("Shift+F10");
  }
  const assetId = await card.getAttribute("data-h3-nle-asset");
  const menu = page.locator(`[data-h3-nle-asset-menu="${assetId}"]`);
  await expect(menu).toBeVisible();
  await expect(menu.locator('[role="menuitem"]').first()).toBeFocused();
  return menu;
}

/** Chooses one asset-menu command by its control value, by pointer or by keyboard. */
export async function chooseAssetCommand(
  page: Page,
  card: Locator,
  control: "asset.insert" | "range.insert" | "range.overwrite",
  via: Via = "pointer",
): Promise<void> {
  const menu = await openAssetMenu(page, card, via);
  const item = menu.locator(`[data-h3-nle-control="${control}"]`);
  if (via === "pointer") await item.click();
  else {
    const order = ["asset.insert", "range.insert", "range.overwrite"];
    for (let step = 0; step < order.indexOf(control); step += 1)
      await page.keyboard.press("ArrowDown");
    await expect(item).toBeFocused();
    await page.keyboard.press("Enter");
  }
  await expect(menu).toHaveCount(0);
}
