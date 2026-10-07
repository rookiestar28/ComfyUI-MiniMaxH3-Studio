// M25-79: the timeline's clip and track menus close when a press lands outside them, with either
// mouse button, in the timeline or in another editor area. Before the repair they stayed open after
// every such press while focus moved on, so Escape then closed the whole editor around a menu that
// was still mounted. A press sends no command, and a trigger button toggles its own menu.

import { expect, test, type Page } from "@playwright/test";

import { canonicalWorkspace, snapshot, surface } from "../helpers/nleCanonical";

test.use({ viewport: { width: 1402, height: 868 }, deviceScaleFactor: 1 });

const clipMenu = (page: Page) => page.getByRole("menu", { name: "Clip menu" });
const trackMenu = (page: Page) =>
  page.getByRole("menu", { name: "Track menu" });

const mainTrack = (page: Page) =>
  page.locator(surface).locator('[data-h3-nle-track="track-0"]');

const openers = {
  "the clip menu from a right-click on a clip": {
    menu: clipMenu,
    open: (page: Page) =>
      mainTrack(page)
        .locator("[data-h3-nle-clip]")
        .first()
        .locator('[data-h3-nle-control="selection.set"]')
        .click({ button: "right" }),
  },
  "the track menu from its header button": {
    menu: trackMenu,
    open: (page: Page) =>
      mainTrack(page).locator('[data-h3-nle-menu-trigger="track"]').click(),
  },
  "the track menu from a right-click on its header": {
    menu: trackMenu,
    open: (page: Page) =>
      mainTrack(page)
        .locator(".h3-nle-track-header")
        .click({ button: "right", position: { x: 4, y: 4 } }),
  },
};

// One press point inside the timeline (the ruler, clear of the playhead at frame 0) and one in
// another editor area (the inspector's empty body).
const pressPoints = {
  "the ruler": async (page: Page) => {
    const box = (await page
      .locator(surface)
      .locator(".h3-nle-ruler")
      .boundingBox())!;
    return { x: box.x + box.width * 0.75, y: box.y + box.height / 2 };
  },
  "the inspector": async (page: Page) => {
    const box = (await page
      .locator(surface)
      .locator('[data-h3-nle-area="inspector"]')
      .boundingBox())!;
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  },
};

test.describe("M25-79 an outside press closes the timeline menus", () => {
  for (const [opener, { menu, open }] of Object.entries(openers))
    for (const [where, point] of Object.entries(pressPoints))
      for (const button of ["left", "right"] as const)
        test(`${opener} closes on a ${button} press on ${where}`, async ({
          page,
        }) => {
          await canonicalWorkspace(page);
          await open(page);
          await expect(menu(page)).toBeVisible();
          const receipts = (await snapshot(page)).receipts;
          const at = await point(page);
          await page.mouse.click(at.x, at.y, { button });
          await expect(menu(page)).toHaveCount(0);
          await expect(
            page.getByRole("menu", { name: /^(Clip|Track) menu$/ }),
          ).toHaveCount(0);
          await expect(page.locator(surface)).toBeVisible();
          expect((await snapshot(page)).receipts).toBe(receipts);
        });
});

test("M25-79 a second press on the trigger button closes its menu", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const trigger = mainTrack(page).locator('[data-h3-nle-menu-trigger="track"]');
  const box = (await trigger.boundingBox())!;
  // The menu opens at the press, so it covers the trigger below and to the right of that point.
  // Open from the trigger's lower right and press again at its upper left, which stays clear.
  await trigger.click({ position: { x: box.width - 6, y: box.height - 4 } });
  await expect(trackMenu(page)).toBeVisible();
  await trigger.click({ position: { x: 4, y: 3 } });
  await expect(trackMenu(page)).toHaveCount(0);
  await expect(trigger).toBeFocused();
  expect((await snapshot(page)).receipts).toBe(0);
});

test("M25-79 a right-click on another clip leaves exactly that clip's menu open", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const bodies = mainTrack(page).locator(
    '[data-h3-nle-clip] [data-h3-nle-control="selection.set"]',
  );
  await bodies.nth(0).click({ button: "right" });
  await expect(clipMenu(page)).toBeVisible();
  // The second clip must be one the open menu does not cover.
  const menuBox = (await clipMenu(page).boundingBox())!;
  let other = -1;
  for (let index = 1; index < (await bodies.count()); index += 1) {
    const box = await bodies.nth(index).boundingBox();
    if (
      box !== null &&
      box.x > menuBox.x + menuBox.width + 8 &&
      box.x + box.width < 1402 - 8
    ) {
      other = index;
      break;
    }
  }
  expect(other).toBeGreaterThan(0);
  await bodies.nth(other).click({ button: "right" });
  await expect(clipMenu(page)).toHaveCount(1);
  await expect(clipMenu(page)).toBeVisible();
  // A press inside the menu keeps it open.
  await clipMenu(page).getByRole("spinbutton").first().click();
  await expect(clipMenu(page)).toBeVisible();
  expect((await snapshot(page)).receipts).toBe(0);
});

// B-M2564-01 for the timeline menus: once focus leaving a menu closes it, a command that starts
// elsewhere must not. It disables the items, the focused item drops focus and the dialog's focus
// keeper would park it outside; the menu holds focus itself until the command settles. The command
// starts from script, so neither the pointer nor focus leaves the menu. `pendingMs` holds it open.
test("M25-79 an open clip menu survives a command that starts elsewhere", async ({
  page,
}) => {
  await page.goto("/nleWorkspace.html?pendingMs=1500");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator(surface)).toBeVisible();
  await mainTrack(page)
    .locator('[data-h3-nle-clip] [data-h3-nle-control="selection.set"]')
    .first()
    .click({ button: "right" });
  const first = clipMenu(page).locator('[role="menuitem"]').first();
  await expect(first).toBeFocused();
  // Lock the last track, not the clip's own: a locked clip track would keep the trims disabled.
  await page.evaluate(() =>
    [
      ...document.querySelectorAll<HTMLButtonElement>(
        '[data-h3-nle-track] [data-h3-nle-control="track.locked"]',
      ),
    ]
      .at(-1)!
      .click(),
  );
  await expect(first).toBeDisabled();
  await expect(clipMenu(page)).toBeVisible();
  await expect(clipMenu(page)).toBeFocused();
  await expect(first).toBeEnabled({ timeout: 10_000 });
  await expect(clipMenu(page)).toBeVisible();
  await expect(first).toBeFocused();
});
