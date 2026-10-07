// M25-63: the redesigned media bin on the REAL integrated shell (`frontend/e2e/nleShell.tsx`), whose
// timeline commands are committed by the canonical core. Each moved capability is exercised at its
// new place (A63-3) and the card grid is measured in the browser's layout engine (A63-5).

import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  chooseAssetCommand,
  chooseMediaOption,
  type Via,
} from "../helpers/nleBinMenus";
import {
  openIntegratedShell,
  shellSnapshot,
  shellSurface,
  type ShellOracle,
} from "../helpers/nleShell";

test.use({ viewport: { width: 1632, height: 932 }, deviceScaleFactor: 1 });

type Kinded = Readonly<{ commands: readonly Readonly<{ kind: string }>[] }>;

const kinds = (oracle: ShellOracle, from: number) =>
  oracle.transactions
    .slice(from)
    .map((transaction) => (transaction as Kinded).commands[0]!.kind);

async function openBin(page: Page) {
  const oracle = await openIntegratedShell(page, "reference-preload");
  const overlay = page.locator(shellSurface);
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await expect(overlay.locator("[data-h3-nle-asset]")).toHaveCount(4);
  return { oracle, overlay };
}

const count = (overlay: Locator) =>
  overlay.locator("[data-h3-nle-media-count]");
// The timeline toolbar's overflow menu is always in the DOM (hidden), so the bin's sort menu is
// named, never the first `[role="menu"]`.
const sortMenu = (page: Page) =>
  page.locator('[role="menu"][aria-label="Sort and filter"]');

/**
 * B-M2563-02: a bin menu extends past the bin over the splitter and the monitor. Every item must be
 * the element under its own centre, or a press there lands on whatever paints above the menu.
 */
async function expectMenuHitTestable(menu: Locator) {
  const hits = await menu.evaluate((root) =>
    [...root.querySelectorAll<HTMLElement>('[role^="menuitem"]')].map(
      (item) => {
        const box = item.getBoundingClientRect();
        const hit = document.elementFromPoint(
          box.left + box.width / 2,
          box.top + box.height / 2,
        );
        return {
          item: item.textContent,
          right: Math.round(box.right),
          own: hit !== null && item.contains(hit),
          hit: hit === null ? null : `${hit.tagName}.${hit.className}`,
        };
      },
    ),
  );
  expect(hits.length).toBeGreaterThan(0);
  for (const hit of hits) expect(hit.own, JSON.stringify(hit)).toBe(true);
  return hits;
}

/**
 * B-M2563-10: the sort and filter menu reads as a menu. Each radio group has a visible heading and
 * lists its options in one column, and the chosen option of each group, and only that one, shows a
 * check. Measured in the browser, where a group of inline buttons wraps into rows.
 */
async function expectMenuReadsAsMenu(menu: Locator) {
  const groups = await menu.evaluate((root) =>
    [...root.querySelectorAll<HTMLElement>('[role="group"]')].map((group) => {
      const visible = (node: Element) => {
        const box = node.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
      };
      const heading = group.querySelector("[data-h3-nle-menu-heading]");
      return {
        group: group.getAttribute("data-h3-nle-control"),
        heading:
          heading !== null && visible(heading) ? heading.textContent : null,
        items: [
          ...group.querySelectorAll<HTMLElement>('[role="menuitemradio"]'),
        ].map((item) => {
          const box = item.getBoundingClientRect();
          const check = item.querySelector('[data-h3-nle-icon="check"]');
          return {
            value: item.getAttribute("data-h3-nle-value"),
            checked: item.getAttribute("aria-checked") === "true",
            marked: check !== null && visible(check),
            left: Math.round(box.left),
            top: Math.round(box.top),
            bottom: Math.round(box.bottom),
            width: Math.round(box.width),
          };
        }),
      };
    }),
  );
  expect(groups.map((group) => [group.group, group.heading])).toEqual([
    ["media.sort", "Sort media"],
    ["media.filter", "Filter media"],
  ]);
  for (const group of groups) {
    const [first, ...rest] = group.items;
    expect(first, JSON.stringify(group)).toBeDefined();
    let above = first!.bottom;
    for (const item of rest) {
      // One column: the same left edge and width, each option below the one before it.
      expect([item.left, item.width], JSON.stringify(group)).toEqual([
        first!.left,
        first!.width,
      ]);
      expect(item.top, JSON.stringify(group)).toBeGreaterThanOrEqual(above);
      above = item.bottom;
    }
    expect(
      group.items.map((item) => item.marked),
      JSON.stringify(group),
    ).toEqual(group.items.map((item) => item.checked));
    expect(group.items.filter((item) => item.checked)).toHaveLength(1);
  }
}

test("A63-3: search, every sort and filter option by pointer and keyboard, and the view pair", async ({
  page,
}) => {
  const { overlay } = await openBin(page);
  const cards = overlay.locator("[data-h3-nle-asset]");
  await expect(count(overlay)).toHaveText("All media · 4");
  // Search keeps its control and filters by the card name.
  await overlay.locator('[data-h3-nle-control="media.search"]').fill("Clip 02");
  await expect(cards).toHaveCount(1);
  await expect(count(overlay)).toHaveText("All media · 1");
  await overlay.locator('[data-h3-nle-control="media.search"]').fill("");
  // Every filter, alternating pointer and keyboard.
  const filters: readonly (readonly [string, Via, number, string])[] = [
    ["video", "pointer", 3, "Videos · 3"],
    ["image", "keyboard", 1, "Pictures · 1"],
    ["added", "pointer", 0, "Added to timeline · 0"],
    ["all", "keyboard", 4, "All media · 4"],
  ];
  for (const [value, via, cardCount, line] of filters) {
    await chooseMediaOption(overlay, "media.filter", value, via);
    await expect(cards).toHaveCount(cardCount);
    await expect(count(overlay)).toHaveText(line);
  }
  // Both sort orders, by keyboard and by pointer; the checked item follows the choice.
  await chooseMediaOption(overlay, "media.sort", "ordinal_desc", "keyboard");
  await expect(cards.first()).toHaveAttribute("data-h3-nle-card-index", "4");
  await overlay.locator('[data-h3-nle-control="media.sort_filter"]').click();
  await expect(
    page.locator('[data-h3-nle-control="media.sort"] [aria-checked="true"]'),
  ).toHaveAttribute("data-h3-nle-value", "ordinal_desc");
  await expectMenuHitTestable(sortMenu(page));
  await expectMenuReadsAsMenu(sortMenu(page));
  await page.keyboard.press("Escape");
  await expect(sortMenu(page)).toHaveCount(0);
  await chooseMediaOption(overlay, "media.sort", "ordinal_asc", "pointer");
  await expect(cards.first()).toHaveAttribute("data-h3-nle-card-index", "1");
  // A press outside closes the menu without choosing.
  await overlay.locator('[data-h3-nle-control="media.sort_filter"]').click();
  await overlay.locator("[data-h3-nle-media-count]").click();
  await expect(sortMenu(page)).toHaveCount(0);
  await expect(cards.first()).toHaveAttribute("data-h3-nle-card-index", "1");
  // The view pair: pressed state by keyboard and pointer.
  const list = overlay.locator('[data-h3-nle-control="media.view.list"]');
  const grid = overlay.locator('[data-h3-nle-control="media.view.grid"]');
  await list.focus();
  await page.keyboard.press("Enter");
  await expect(overlay.locator("[data-h3-nle-media-view]")).toHaveAttribute(
    "data-h3-nle-media-view",
    "list",
  );
  await expect(list).toHaveAttribute("aria-pressed", "true");
  await grid.click();
  await expect(overlay.locator("[data-h3-nle-media-view]")).toHaveAttribute(
    "data-h3-nle-media-view",
    "grid",
  );
  await expect(grid).toHaveAttribute("aria-pressed", "true");
});

test("A63-3: + and the card menu append Add and place Insert/Overwrite by pointer and keyboard", async ({
  page,
}) => {
  const { oracle, overlay } = await openBin(page);
  const card = (index: number) =>
    overlay.locator(`[data-h3-nle-card-index="${index}"]`);
  let sent = oracle.transactions.length;
  // Each command lands on the empty zero-clip timeline: after it commits, one Undo restores that
  // state, so Add appends to the empty primary track and range commands use the playhead.
  const commits = async (kind: string) => {
    await expect.poll(() => kinds(oracle, sent)).toEqual([kind]);
    await overlay.locator('[data-h3-nle-control="history.undo"]').click();
    await expect.poll(() => kinds(oracle, sent)).toEqual([kind, "undo"]);
    await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(0);
    sent = oracle.transactions.length;
  };
  // "+" by pointer: hover reveals it over the thumbnail, one click is one insert.
  await card(1).hover();
  const plus = card(1).locator('[data-h3-nle-control="asset.insert"]');
  await expect(plus).toHaveCSS("opacity", "1");
  await expect(plus).toHaveAttribute(
    "aria-label",
    "Add Clip 01 to the timeline",
  );
  await expect(plus).toHaveAttribute(
    "title",
    "Append to the end of the primary video track",
  );
  await plus.click();
  await commits("insert_asset_clip");
  // "+" by keyboard: Tab from the card reaches it, and it shows while focus is inside the card.
  await card(2).locator(".h3-nle-media-primary").focus();
  await page.keyboard.press("Tab");
  const plusTwo = card(2).locator('[data-h3-nle-control="asset.insert"]');
  await expect(plusTwo).toBeFocused();
  await expect(plusTwo).toHaveCSS("opacity", "1");
  await page.keyboard.press("Enter");
  await commits("insert_asset_clip");
  // The card menu by pointer (right-click) and by keyboard (Shift+F10), every command, on a
  // video card: card 3 is a picture, which has no image-overlay track to go to in this project,
  // so the unchanged admission (admitTimelineInsert) sends nothing for it.
  for (const [control, kind, via] of [
    ["range.insert", "insert_range", "pointer"],
    ["range.overwrite", "overwrite_range", "keyboard"],
    ["asset.insert", "insert_asset_clip", "keyboard"],
    ["range.insert", "insert_range", "keyboard"],
    ["range.overwrite", "overwrite_range", "pointer"],
    ["asset.insert", "insert_asset_clip", "pointer"],
  ] as const) {
    await chooseAssetCommand(page, card(4), control, via);
    await commits(kind);
    // The menu returns focus to the card; the card is disabled while the edit is in flight, so the
    // timeline takes focus as it does for every bin command (NleTimeline's removed-focus guard).
    // Focus never leaves the editor.
    expect(
      await overlay.evaluate((dialog) =>
        dialog.contains(document.activeElement),
      ),
    ).toBe(true);
  }
  // B-M2563-02: a menu opened on the right-hand card crosses the splitter into the monitor, and
  // every item stays pressable there.
  const binRight = await overlay
    .locator('[data-h3-nle-area="bin"]')
    .evaluate((bin) => bin.getBoundingClientRect().right);
  await card(2).locator(".h3-nle-media-primary").click({ button: "right" });
  const crossing = await expectMenuHitTestable(
    page.locator("[data-h3-nle-asset-menu]"),
  );
  expect(Math.max(...crossing.map((hit) => hit.right))).toBeGreaterThan(
    binRight,
  );
  await page.keyboard.press("Escape");
  // Escape closes the menu with nothing sent.
  await card(4).locator(".h3-nle-media-primary").focus();
  await page.keyboard.press("Shift+F10");
  await expect(page.locator("[data-h3-nle-asset-menu]")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("[data-h3-nle-asset-menu]")).toHaveCount(0);
  await expect(card(4).locator(".h3-nle-media-primary")).toBeFocused();
  expect(kinds(oracle, sent)).toEqual([]);
  // Eight commands, each followed by its Undo.
  expect((await shellSnapshot(page)).receipts).toBe(16);
});

test("A63-5: two card columns at the default 1600 px overlay, one at the bin minimum, pills apart", async ({
  page,
}) => {
  const { overlay } = await openBin(page);
  const layout = () =>
    overlay.evaluate((dialog) => {
      const grid = dialog.querySelector<HTMLElement>(".h3-nle-media-grid")!;
      const bin = dialog.querySelector('[data-h3-nle-area="bin"]')!;
      const cards = [...grid.querySelectorAll("[data-h3-nle-asset]")].map(
        (card) => {
          const box = (selector: string) => {
            const element = card.querySelector(selector);
            if (element === null) return null;
            const rect = element.getBoundingClientRect();
            return {
              left: rect.left,
              right: rect.right,
              top: rect.top,
              bottom: rect.bottom,
            };
          };
          return {
            left: Math.round(card.getBoundingClientRect().left),
            art: box(".h3-nle-media-art"),
            added: box('[data-h3-nle-media-badge="added"]'),
            duration: box('[data-h3-nle-media-badge="duration"]'),
          };
        },
      );
      return {
        dialog: Math.round(dialog.getBoundingClientRect().width),
        bin: Math.round(bin.getBoundingClientRect().width),
        columns: new Set(cards.map((card) => card.left)).size,
        cards,
      };
    });
  const initial = await layout();
  expect(initial.dialog, JSON.stringify(initial)).toBe(1600);
  expect(initial.columns, JSON.stringify(initial)).toBe(2);
  // Add one clip so a card carries both pills, then check that no two pills overlap.
  await overlay
    .locator(
      '[data-h3-nle-card-index="1"] [data-h3-nle-control="asset.insert"]',
    )
    .click();
  await expect(
    overlay.locator(
      '[data-h3-nle-card-index="1"] [data-h3-nle-media-badge="added"]',
    ),
  ).toBeVisible();
  const pills = await layout();
  for (const card of pills.cards) {
    if (card.added === null || card.duration === null) continue;
    expect(card.added.right, JSON.stringify(card)).toBeLessThanOrEqual(
      card.duration.left,
    );
    expect(card.added.left).toBeGreaterThanOrEqual(card.art!.left);
    expect(card.duration.right).toBeLessThanOrEqual(card.art!.right);
  }
  // The bin at its minimum: one column.
  await overlay.locator('[data-h3-nle-splitter="bin_monitor"]').focus();
  await page.keyboard.press("Home");
  // The card grid follows the bin's ResizeObserver, one frame after the splitter moves.
  await expect.poll(async () => (await layout()).columns).toBe(1);
  const narrow = await layout();
  expect(narrow.columns, JSON.stringify(narrow)).toBe(1);
  expect(narrow.bin, JSON.stringify(narrow)).toBeLessThan(initial.bin);
  // B-M2563-02: at the bin minimum the sort menu lies mostly over the monitor.
  await overlay.locator('[data-h3-nle-control="media.sort_filter"]').click();
  await expectMenuHitTestable(sortMenu(page));
  await expectMenuReadsAsMenu(sortMenu(page));
  await page.keyboard.press("Escape");
});

test("A63-7 (B-M2563-03, B-M2563-04): the bin and top bar render as designed and no pill is cut", async ({
  page,
}) => {
  const { overlay } = await openBin(page);
  await overlay
    .locator(
      '[data-h3-nle-card-index="1"] [data-h3-nle-control="asset.insert"]',
    )
    .click();
  await expect(
    overlay.locator(
      '[data-h3-nle-card-index="1"] [data-h3-nle-media-badge="added"]',
    ),
  ).toBeVisible();
  await page.mouse.move(0, 0);
  const chrome = await overlay.evaluate((dialog) => {
    const resolve = (token: string) => {
      const probe = document.createElement("span");
      probe.style.backgroundColor = `var(${token})`;
      dialog.append(probe);
      const value = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return value;
    };
    const face = (element: Element) => {
      const style = getComputedStyle(element);
      return {
        border: style.borderTopWidth,
        shadow: style.boxShadow,
        background: style.backgroundColor,
        height: Math.round(element.getBoundingClientRect().height),
      };
    };
    const one = (selector: string) => face(dialog.querySelector(selector)!);
    return {
      transparent: "rgba(0, 0, 0, 0)",
      surface3: resolve("--h3-surface-3"),
      canvas: resolve("--h3-canvas"),
      header: {
        ...one(".h3-nle-header"),
        rule: getComputedStyle(dialog.querySelector(".h3-nle-header")!)
          .borderBottomWidth,
      },
      gripGlyph: Math.round(
        dialog
          .querySelector('[data-h3-nle-action="resize"] [data-h3-nle-icon]')!
          .getBoundingClientRect().width,
      ),
      close: one('[data-h3-nle-action="close"]'),
      tabs: [...dialog.querySelectorAll('[role="tab"][id^="h3-nle-tab-"]')].map(
        face,
      ),
      tabGlyphs: [
        ...dialog.querySelectorAll('[role="tab"][id^="h3-nle-tab-"]'),
      ].map((tab) => [
        tab.getAttribute("data-h3-nle-pane"),
        tab
          .querySelector("[data-h3-nle-icon]")
          ?.getAttribute("data-h3-nle-icon"),
      ]),
      sortFilter: one('[data-h3-nle-control="media.sort_filter"]'),
      list: one('[data-h3-nle-control="media.view.list"]'),
      card: one("[data-h3-nle-asset] .h3-nle-media-primary"),
      art: one("[data-h3-nle-asset] .h3-nle-media-art"),
      plus: one('[data-h3-nle-asset] [data-h3-nle-control="asset.insert"]'),
      search: one(".h3-nle-media-search"),
      searchInput: one(".h3-nle-media-search input"),
      pills: [...dialog.querySelectorAll("[data-h3-nle-media-badge]")].map(
        (pill) => ({
          kind: pill.getAttribute("data-h3-nle-media-badge"),
          text: pill.textContent,
          title: pill.getAttribute("title"),
          cut: pill.scrollWidth > pill.clientWidth,
        }),
      ),
    };
  });
  const evidence = JSON.stringify(chrome);
  // No frame, lift shadow or control face on the chrome-less buttons (B-M2563-03).
  for (const control of [
    chrome.close,
    ...chrome.tabs,
    chrome.sortFilter,
    chrome.list,
    chrome.card,
    chrome.plus,
  ]) {
    expect(control.border, evidence).toBe("0px");
    expect(control.shadow, evidence).toBe("none");
  }
  for (const control of [
    chrome.close,
    ...chrome.tabs,
    chrome.sortFilter,
    chrome.list,
    chrome.card,
  ])
    expect(control.background, evidence).toBe(chrome.transparent);
  // B-M2563-12: the top bar sits on the page colour with no rule under it, as in the canvas,
  // and the corner grip draws the plan's 14 px glyph (row 17).
  expect(chrome.header.background, evidence).toBe(chrome.canvas);
  expect(chrome.header.rule, evidence).toBe("0px");
  expect(chrome.gripGlyph, evidence).toBe(14);
  // The thumbnail sits on surface 3, a defined token; one frame around the search field, and
  // its input fills the field (B-M2563-03).
  expect(chrome.art.background, evidence).toBe(chrome.surface3);
  expect(chrome.art.background, evidence).not.toBe(chrome.transparent);
  expect(chrome.searchInput.border, evidence).toBe("0px");
  expect(chrome.searchInput.background, evidence).toBe(chrome.transparent);
  expect(chrome.searchInput.height, evidence).toBe(chrome.search.height);
  // Each bin tab shows its own glyph; Text is not the "+" of an add action (B-M2563-04).
  expect(chrome.tabGlyphs, evidence).toEqual([
    ["assets", "media"],
    ["text", "text"],
    ["sequence", "storyboard"],
  ]);
  // Every pill shows its whole text: "Added" and a compact duration, the full sentence and
  // timecode in the tooltip (B-M2563-04).
  expect(chrome.pills.length).toBeGreaterThan(4);
  for (const pill of chrome.pills) {
    expect(pill.cut, evidence).toBe(false);
    if (pill.kind === "added") {
      expect(pill.text).toBe("Added");
      expect(pill.title).toBe("Added to timeline");
    } else {
      expect(pill.text).toMatch(/^(?:\d+:)?\d{2}:\d{2}$|^\d+f$/u);
      expect(pill.title).toMatch(/^\d{2}:\d{2}:\d{2}:\d{2}$/u);
    }
  }
});
