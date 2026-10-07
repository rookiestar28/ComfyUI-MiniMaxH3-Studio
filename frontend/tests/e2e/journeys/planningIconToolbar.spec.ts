import { expect, test, type Page } from "../fixtures/h3Page";

/**
 * M25-41: the whole-video sequence planning actions are icon buttons with one description tooltip
 * per group. M25-43: they tile one shared set of five equal columns across every group, the full
 * row reaching the right edge, and the pointer shows each action's availability. Measured in a real layout engine on the
 * shipped stylesheet, because a crowded row of wrapping text buttons satisfies every "no horizontal
 * overflow" check the sidebar already had.
 */

const LOCALES = ["en", "zh-TW", "zh-CN"] as const;
const WIDTHS = [480, 704, 960] as const;
const SETTINGS_TAB = /^(?:Settings|設定|设置)$/;
const LANGUAGE_FIELD = /^(?:Language|語言|语言)$/;
const PRODUCTION_TAB = /^(?:Production|導演台|导演台)$/;
const MAIN = [
  "planning.prepare_context",
  "planning.admit_canonical",
  "planning.review_storyboard",
  "planning.propose",
  "planning.approve_import",
] as const;

async function setLocale(page: Page, locale: string): Promise<void> {
  await page.getByRole("button", { name: SETTINGS_TAB }).click();
  await page.getByLabel(LANGUAGE_FIELD).selectOption(locale);
  await expect(page.locator("section.h3c")).toHaveAttribute("lang", locale);
}

async function setSidebarWidth(page: Page, width: number): Promise<void> {
  await page.setViewportSize({ width: width + 360, height: 1100 });
  await page
    .locator("#production-sidebar-container")
    .evaluate((node, value) => {
      (node as HTMLElement).style.width = `${value}px`;
      (node as HTMLElement).style.maxWidth = "none";
    }, width);
  await expect(page.locator("#production-sidebar-container")).toHaveCSS(
    "width",
    `${width}px`,
  );
}

type Box = Readonly<{
  left: number;
  right: number;
  top: number;
  bottom: number;
}>;

type Geometry = Readonly<{
  groups: readonly {
    label: string | null;
    row: Box;
    rowOverflow: number;
    chip: Box | null;
    chipOverflow: number;
    buttons: readonly {
      control: string | null;
      x: number;
      y: number;
      width: number;
      height: number;
      text: string;
      overflow: number;
      icon: boolean;
    }[];
  }[];
  fields: readonly {
    label: DOMRect;
    control: DOMRect;
  }[];
  /** The field block and every action group, in document order. */
  blocks: readonly { name: string; top: number; bottom: number }[];
  shellOverflow: number;
}>;

async function geometry(page: Page, scope: string): Promise<Geometry> {
  return page.locator(scope).evaluate((root) => {
    const rect = (node: Element) => node.getBoundingClientRect().toJSON();
    const groups = [...root.querySelectorAll(".h3-icon-group")].map(
      (group) => ({
        label: group.getAttribute("aria-label"),
        row: rect(group.querySelector(".h3-icon-row")!),
        rowOverflow:
          group.querySelector(".h3-icon-row")!.scrollWidth -
          group.querySelector(".h3-icon-row")!.clientWidth,
        chip: group.querySelector(".h3-nle-chip")
          ? rect(group.querySelector(".h3-nle-chip")!)
          : null,
        chipOverflow: group.querySelector(".h3-nle-chip")
          ? group.querySelector(".h3-nle-chip")!.scrollWidth -
            group.querySelector(".h3-nle-chip")!.clientWidth
          : 0,
        buttons: [...group.querySelectorAll("button.h3-icon-button")].map(
          (button) => {
            const box = button.getBoundingClientRect();
            return {
              control: button.getAttribute("data-h3-nle-control"),
              x: box.x,
              y: box.y,
              width: box.width,
              height: box.height,
              text: (button.textContent ?? "").trim(),
              overflow: button.scrollWidth - button.clientWidth,
              icon: button.querySelector("svg[aria-hidden='true']") !== null,
            };
          },
        ),
      }),
    );
    const fields = [...root.querySelectorAll(".h3-plan-field")].map(
      (field) => ({
        label: rect(field.querySelector("span")!),
        control: rect(field.querySelector("input, select")!),
      }),
    );
    const blocks = [
      ...root.querySelectorAll(".h3-plan-fields, .h3-icon-group"),
    ].map((node) => {
      const box = node.getBoundingClientRect();
      return {
        name: node.getAttribute("aria-label") ?? node.className,
        top: box.top,
        bottom: box.bottom,
      };
    });
    const shell = root.closest(".h3c") ?? root;
    return {
      groups,
      fields,
      blocks,
      shellOverflow: shell.scrollWidth - shell.clientWidth,
    };
  }) as Promise<Geometry>;
}

function assertToolbarGeometry(
  result: Geometry,
  where: string,
  perLine: number = MAIN.length,
): void {
  expect(result.shellOverflow, `${where} shell overflow`).toBeLessThanOrEqual(
    0,
  );
  expect(result.groups.length, `${where} groups`).toBeGreaterThanOrEqual(2);
  // The shared columns are the five-action row's tiles: it spans its row edge to edge. A row
  // narrower than five 44 px columns (252 px) keeps the same grid with fewer columns and wraps; the
  // caller pins which case each measured row is, so neither form passes for the other.
  const main = result.groups.find((group) =>
    group.buttons.some((button) => button.control === MAIN[0]),
  );
  expect(main, `${where} planning actions`).toBeDefined();
  const width = main!.row.right - main!.row.left;
  if (perLine === MAIN.length)
    expect(width, `${where} row fits five tiles`).toBeGreaterThanOrEqual(252);
  else expect(width, `${where} row narrower than five tiles`).toBeLessThan(252);
  const flow = [...main!.buttons].sort((a, b) => a.y - b.y || a.x - b.x);
  expect(
    flow.map((button) => button.control),
    `${where} planning actions in reading order`,
  ).toEqual([...MAIN]);
  expect(
    new Set(flow.map((button) => Math.round(button.y))).size,
    `${where} lines of ${perLine}`,
  ).toBe(Math.ceil(MAIN.length / perLine));
  const columns = flow.slice(0, perLine);
  expect(
    new Set(columns.map((button) => Math.round(button.y))).size,
    `${where} first line holds ${perLine}`,
  ).toBe(1);
  expect(
    Math.abs(columns[0]!.x - main!.row.left),
    `${where} first tile at the row's left edge`,
  ).toBeLessThanOrEqual(1);
  const last = columns[columns.length - 1]!;
  expect(
    Math.abs(last.x + last.width - main!.row.right),
    `${where} last tile at the row's right edge`,
  ).toBeLessThanOrEqual(1);
  const tile = columns[0]!.width;
  for (let index = 1; index < columns.length; index += 1)
    expect(
      Math.abs(
        columns[index]!.x -
          (columns[index - 1]!.x + columns[index - 1]!.width) -
          8,
      ),
      `${where} ${columns[index]!.control} fixed gap`,
    ).toBeLessThanOrEqual(1);
  for (const group of result.groups) {
    expect(
      group.rowOverflow,
      `${where} ${group.label} row overflow`,
    ).toBeLessThanOrEqual(0);
    expect(
      Math.abs(group.row.left - main!.row.left) +
        Math.abs(group.row.right - main!.row.right),
      `${where} ${group.label} spans the same width`,
    ).toBeLessThanOrEqual(1);
    const ordered = [...group.buttons].sort((a, b) => a.y - b.y || a.x - b.x);
    ordered.forEach((button, index) => {
      // Shorter rows start at the left on the same column lines.
      if (group !== main)
        expect(
          Math.abs(button.x - columns[index % perLine]!.x),
          `${where} ${button.control} on column ${index + 1}`,
        ).toBeLessThanOrEqual(1);
    });
    if (group.chip !== null) {
      expect(
        Math.abs(group.chip.left - columns[ordered.length % perLine]!.x),
        `${where} ${group.label} chip starts on the next column`,
      ).toBeLessThanOrEqual(1);
      expect(
        Math.abs(group.chip.right - main!.row.right),
        `${where} ${group.label} chip reaches the right edge`,
      ).toBeLessThanOrEqual(1);
      expect(
        group.chipOverflow,
        `${where} chip text overflow`,
      ).toBeLessThanOrEqual(0);
    }
  }
  for (const group of result.groups) {
    const buttons = [...group.buttons].sort((a, b) => a.y - b.y || a.x - b.x);
    for (const button of buttons) {
      const id = `${where} ${button.control}`;
      expect(
        Math.abs(button.width - tile),
        `${id} shared tile width`,
      ).toBeLessThanOrEqual(1);
      expect(button.width, `${id} target width`).toBeGreaterThanOrEqual(44);
      expect(button.height, `${id} height`).toBeCloseTo(44, 0);
      expect(button.text, `${id} visible text`).toBe("");
      expect(button.icon, `${id} icon`).toBe(true);
      expect(button.overflow, `${id} overflow`).toBeLessThanOrEqual(0);
    }
    for (let index = 1; index < buttons.length; index += 1) {
      const previous = buttons[index - 1]!;
      const current = buttons[index]!;
      if (Math.abs(current.y - previous.y) < 1) {
        expect(
          current.x - (previous.x + previous.width),
          `${where} ${current.control} horizontal gap`,
        ).toBeGreaterThanOrEqual(7.5);
      } else {
        expect(
          current.y - (previous.y + previous.height),
          `${where} ${current.control} vertical gap`,
        ).toBeGreaterThanOrEqual(7.5);
      }
    }
  }
  for (let index = 1; index < result.blocks.length; index += 1) {
    const previous = result.blocks[index - 1]!;
    const current = result.blocks[index]!;
    expect(
      current.top - previous.bottom,
      `${where} ${previous.name} -> ${current.name} vertical gap`,
    ).toBeGreaterThanOrEqual(7.5);
  }
  for (const field of result.fields) {
    expect(
      field.control.x - (field.label.x + field.label.width),
      `${where} label/control separation`,
    ).toBeGreaterThanOrEqual(8);
    expect(
      Math.abs(
        field.control.y +
          field.control.height / 2 -
          (field.label.y + field.label.height / 2),
      ),
      `${where} label/control alignment`,
    ).toBeLessThanOrEqual(6);
  }
}

async function tooltipWithin(
  page: Page,
  scope: string,
): Promise<{ text: string; inside: boolean }> {
  const tip = page.locator(`${scope} [role='tooltip']:not([hidden])`);
  await expect(tip).toHaveCount(1);
  return tip.evaluate((node, selector) => {
    const box = node.getBoundingClientRect();
    const boundary = document
      .querySelector(selector)!
      .closest(".h3c")!
      .getBoundingClientRect();
    const row = node
      .closest(".h3-icon-group")!
      .querySelector(".h3-icon-row")!
      .getBoundingClientRect();
    // The box that can hide it: the nearest clipping ancestor (a scroll pane), else the viewport.
    let clip = { top: 0, bottom: innerHeight };
    for (let parent = node.parentElement; parent; parent = parent.parentElement)
      if (getComputedStyle(parent).overflowY !== "visible") {
        clip = parent.getBoundingClientRect();
        break;
      }
    return {
      text: node.textContent ?? "",
      // Inside the panel's width, fully visible, and never over the actions it describes.
      inside:
        box.width > 0 &&
        box.left >= boundary.left - 0.5 &&
        box.right <= boundary.right + 0.5 &&
        box.top >= Math.max(clip.top, 0) - 0.5 &&
        box.bottom <= Math.min(clip.bottom, innerHeight) + 0.5 &&
        (box.bottom <= row.top + 0.5 || box.top >= row.bottom - 0.5),
    };
  }, scope);
}

/** Every action's position in the section, so a description that shifts the layout is caught. */
async function controlPositions(
  page: Page,
  scope: string,
): Promise<readonly string[]> {
  return page.locator(`${scope} [data-h3-nle-control]`).evaluateAll((nodes) =>
    nodes.map((node) => {
      const box = node.getBoundingClientRect();
      return `${node.getAttribute("data-h3-nle-control")}@${Math.round(box.x)},${Math.round(box.y)}`;
    }),
  );
}

test.describe("M25-41 sidebar planning toolbar", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/?mode=production&planning=1");
  });

  test("icon actions tile a shared five-column grid in three locales at five widths", async ({
    page,
  }) => {
    const section = "[data-h3-nle-planning-status]";
    for (const locale of LOCALES) {
      await setLocale(page, locale);
      await page.getByRole("button", { name: PRODUCTION_TAB }).click();
      await expect(page.locator(section)).toBeVisible();
      for (const width of [...WIDTHS, 713, 1100])
        for (const theme of ["dark", "light"] as const) {
          await setSidebarWidth(page, width);
          await page.locator("section.h3c").evaluate((node, value) => {
            if (value === "light") node.setAttribute("data-theme", "light");
            else node.removeAttribute("data-theme");
          }, theme);
          assertToolbarGeometry(
            await geometry(page, section),
            `${locale} @ ${width} ${theme}`,
          );
          const hues = await page.evaluate(
            (ids) =>
              ids.map(
                (id) =>
                  getComputedStyle(
                    document.querySelector(`[data-h3-nle-control="${id}"]`)!,
                  ).color,
              ),
            [...MAIN],
          );
          expect(new Set(hues).size, `${locale} @ ${width} ${theme} hues`).toBe(
            5,
          );
        }
    }
  });

  test("descriptions appear on hover, on unavailable actions and on keyboard focus, inside the sidebar", async ({
    page,
  }) => {
    const section = "[data-h3-nle-planning-status]";
    await page.getByRole("button", { name: PRODUCTION_TAB }).click();
    for (const width of WIDTHS) {
      await setSidebarWidth(page, width);
      const prepare = page.locator(
        `[data-h3-nle-control="planning.prepare_context"]`,
      );
      // Scroll first, so a position change measures the description and not the scroll.
      await prepare.scrollIntoViewIfNeeded();
      const resting = await controlPositions(page, section);
      await prepare.hover();
      const hovered = await tooltipWithin(page, section);
      expect(hovered.text).toContain("Context");
      expect(hovered.inside, `hover inside @ ${width}`).toBe(true);
      // A description overlays the section: no action moves while it is visible, so a pointer
      // travelling to the next group lands on the action it was aimed at.
      expect(
        await controlPositions(page, section),
        `no action moves @ ${width}`,
      ).toEqual(resting);
      // Straight from the action onto its description, across the gap between them.
      const tipBox = await page
        .locator(`${section} [role='tooltip']:not([hidden])`)
        .boundingBox();
      const prepareBox = await prepare.boundingBox();
      await page.mouse.move(
        prepareBox!.x + prepareBox!.width / 2,
        tipBox!.y + tipBox!.height / 2,
        { steps: 12 },
      );
      await expect(
        page.locator(`${section} [role='tooltip']:not([hidden])`),
        `the pointer can rest on the description @ ${width}`,
      ).toContainText("Context");
      await prepare.hover();
      await expect(
        page.locator(`${section} [role='tooltip']:not([hidden])`),
      ).toContainText("Context");
      // The action a pointer meets first going down: the nearest icon action below this row.
      const next = await prepare.evaluate((button, scope) => {
        const row = button.closest(".h3-icon-row")!.getBoundingClientRect();
        const below = [
          ...document.querySelectorAll(`${scope} button.h3-icon-button`),
        ]
          .map((node) => ({
            control: node.getAttribute("data-h3-nle-control")!,
            box: node.getBoundingClientRect(),
          }))
          .filter((item) => item.box.top >= row.bottom)
          .sort((a, b) => a.box.top - b.box.top || a.box.left - b.box.left);
        return below[0]!;
      }, section);
      await page.mouse.move(
        next.box.left + next.box.width / 2,
        next.box.top + next.box.height / 2,
        { steps: 12 },
      );
      await expect(
        page.locator(`[data-h3-nle-control="${next.control}"]`),
        `a pointer moved down reaches ${next.control} @ ${width}`,
      ).toHaveAttribute("aria-describedby", /.+/);
      await page.mouse.move(0, 0);

      // Scrolled so the actions sit at the top edge of what is visible, there is no room above:
      // the description opens below instead of being hidden.
      await page.setViewportSize({ width: width + 360, height: 360 });
      const edge = await prepare.evaluate((button) => {
        const group = button.closest(".h3-icon-group")!;
        let scroller: Element | null = group.parentElement;
        while (scroller && getComputedStyle(scroller).overflowY === "visible")
          scroller = scroller.parentElement;
        const clipTop = scroller ? scroller.getBoundingClientRect().top : 0;
        const offset =
          group.getBoundingClientRect().top - Math.max(clipTop, 0) - 2;
        if (scroller) scroller.scrollTop += offset;
        else window.scrollBy(0, offset);
        return group.getBoundingClientRect().top - Math.max(clipTop, 0);
      });
      expect(edge, `actions at the visible top edge @ ${width}`).toBeLessThan(
        8,
      );
      await prepare.hover();
      const atEdge = await tooltipWithin(page, section);
      expect(atEdge.text).toContain("Context");
      expect(atEdge.inside, `edge description visible @ ${width}`).toBe(true);
      await page.mouse.move(0, 0);
      // Undo the edge scroll, or the next width starts with its actions at the edge too.
      await prepare.evaluate((button) => {
        for (let node = button.parentElement; node; node = node.parentElement)
          node.scrollTop = 0;
        window.scrollTo(0, 0);
      });
      await page.setViewportSize({ width: width + 360, height: 1100 });

      const unavailable = page.locator(
        `[data-h3-nle-control="planning.propose"]`,
      );
      await expect(unavailable).toBeDisabled();
      await unavailable.locator("xpath=..").hover();
      const disabledTip = await tooltipWithin(page, section);
      expect(disabledTip.text).toContain("Production segments");
      expect(disabledTip.inside, `disabled hover inside @ ${width}`).toBe(true);

      await page.mouse.move(0, 0);
      await expect(
        page.locator(`${section} [role='tooltip']:not([hidden])`),
      ).toHaveCount(0);

      // A pointer left resting on one group while focus moves into another: one description only.
      await unavailable.locator("xpath=..").hover();
      await tooltipWithin(page, section);
      const addRow = page.locator(`[data-h3-nle-control="planning.add_row"]`);
      await addRow.focus();
      const focused = await tooltipWithin(page, section);
      expect(focused.text).toContain("five-second");
      expect(focused.inside, `focus inside @ ${width}`).toBe(true);
      await page.keyboard.press("Escape");
      await expect(
        page.locator(`${section} [role='tooltip']:not([hidden])`),
      ).toHaveCount(0);
      await addRow.blur();
    }
  });

  // M25-63: planning happens in Production only. The editor's Sequence tab has no planning
  // toolbar and keeps the readiness request and the run controls; the Production cases above keep
  // pinning the toolbar.
  test("the full editor's Sequence tab has no planning toolbar and keeps its run controls", async ({
    page,
  }) => {
    for (const locale of LOCALES) {
      await page.goto(`/nleWorkspace.html?locale=${locale}`);
      await page.getByRole("button", { name: "Open full editor" }).click();
      await page.locator('[data-h3-nle-pane="sequence"]').first().click();
      const tab = page.locator(
        '.h3-nle-dialog [data-h3-nle-region="sequence"]',
      );
      await expect(tab).toBeVisible();
      await expect(
        page.locator(".h3-nle-dialog [data-h3-nle-planning-status]"),
      ).toHaveCount(0);
      const controls = await tab
        .locator("[data-h3-nle-control]")
        .evaluateAll((nodes) =>
          nodes.map((node) => node.getAttribute("data-h3-nle-control")),
        );
      expect(
        controls.filter((control) => control?.startsWith("planning.")),
        locale,
      ).toEqual([]);
      expect(controls, locale).toContain("readiness.request");
      expect(controls, locale).toContain("sequence.start");
      await expect(
        tab.locator('[data-h3-nle-status="planning-location"]'),
      ).toBeVisible();
      await expect(tab.locator("h4").first()).toBeVisible();
    }
  });

  test("a row too narrow for five 44 px tiles wraps without overflow", async ({
    page,
  }) => {
    await page.getByRole("button", { name: PRODUCTION_TAB }).click();
    await setSidebarWidth(page, 704);
    const wrapped = await page
      .locator("[data-h3-nle-planning-status] .h3-icon-group")
      .first()
      .evaluate((group) => {
        (group as HTMLElement).style.inlineSize = "240px";
        const row = group.querySelector(".h3-icon-row")!;
        const boxes = [...row.querySelectorAll("button.h3-icon-button")].map(
          (button) => button.getBoundingClientRect(),
        );
        const result = {
          overflow: row.scrollWidth - row.clientWidth,
          rowRight: row.getBoundingClientRect().right,
          widths: boxes.map((box) => box.width),
          lines: new Set(boxes.map((box) => Math.round(box.top))).size,
          rights: boxes.map((box) => box.right),
        };
        (group as HTMLElement).style.inlineSize = "";
        return result;
      });
    expect(wrapped.overflow).toBeLessThanOrEqual(0);
    expect(wrapped.lines).toBe(2);
    for (const width of wrapped.widths)
      expect(width).toBeGreaterThanOrEqual(44);
    expect(Math.max(...wrapped.rights)).toBeLessThanOrEqual(
      wrapped.rowRight + 1,
    );
  });

  test("the pointer shows not-allowed over unavailable actions and a hand over available ones", async ({
    page,
  }) => {
    type Seen = {
      control: string;
      disabled: boolean;
      cursor: string;
      hitInSlot: boolean;
    };
    const cursors = async (scope: string): Promise<Seen[]> => {
      const buttons = page.locator(`${scope} button.h3-icon-button`);
      const seen: Seen[] = [];
      for (let index = 0; index < (await buttons.count()); index += 1) {
        const button = buttons.nth(index);
        await button.scrollIntoViewIfNeeded();
        const box = (await button.boundingBox())!;
        const x = box.x + box.width / 2;
        const y = box.y + box.height / 2;
        await page.mouse.move(x, y, { steps: 4 });
        seen.push(
          await button.evaluate(
            (node, point) => {
              // The cursor a user sees is the one on the element the pointer hits.
              const hit = document.elementFromPoint(point.x, point.y)!;
              return {
                control: node.getAttribute("data-h3-nle-control")!,
                disabled: (node as HTMLButtonElement).disabled,
                cursor: getComputedStyle(hit).cursor,
                hitInSlot: node.parentElement!.contains(hit),
              };
            },
            { x, y },
          ),
        );
      }
      await page.mouse.move(0, 0);
      return seen;
    };
    const expectCursors = (seen: readonly Seen[], where: string) => {
      for (const entry of seen) {
        expect(entry.hitInSlot, `${where} ${entry.control} hit`).toBe(true);
        expect(entry.cursor, `${where} ${entry.control} cursor`).toBe(
          entry.disabled ? "not-allowed" : "pointer",
        );
      }
    };

    await page.getByRole("button", { name: PRODUCTION_TAB }).click();
    await setSidebarWidth(page, 704);
    const sidebar = await cursors("[data-h3-nle-planning-status]");
    expect(sidebar.some((entry) => entry.disabled)).toBe(true);
    expect(sidebar.some((entry) => !entry.disabled)).toBe(true);
    expectCursors(sidebar, "sidebar @ 704");

    await page.goto("/nleWorkspace.html?locale=en");
    await page.getByRole("button", { name: "Open full editor" }).click();
    await page.locator('[data-h3-nle-pane="sequence"]').first().click();
    // M25-63: the editor's Sequence tab keeps only the readiness request as an icon action.
    const overlaySection = '.h3c [data-h3-nle-region="sequence"]';
    await expect(page.locator(overlaySection)).toBeVisible();
    const overlay = await cursors(overlaySection);
    expect(overlay.length).toBeGreaterThanOrEqual(1);
    expectCursors(overlay, "overlay @ 1280");
  });

  test("forced colors keep every action distinguishable without color", async ({
    page,
  }) => {
    await page.emulateMedia({ forcedColors: "active" });
    await page.getByRole("button", { name: PRODUCTION_TAB }).click();
    await setSidebarWidth(page, 704);
    const section = "[data-h3-nle-planning-status]";
    assertToolbarGeometry(await geometry(page, section), "forced colors @ 704");
    const marks = await page
      .locator(`${section} button.h3-icon-button`)
      .evaluateAll((buttons) =>
        buttons.map((button) => ({
          border: parseFloat(getComputedStyle(button).borderTopWidth),
          name: button.getAttribute("aria-label"),
          icon: button.querySelector("svg path") !== null,
        })),
      );
    for (const mark of marks) {
      expect(mark.border, `${mark.name} border`).toBeGreaterThan(0);
      expect(mark.icon, `${mark.name} icon`).toBe(true);
      expect(mark.name?.length ?? 0).toBeGreaterThan(0);
    }
    await page.emulateMedia({ forcedColors: null });
  });
});
