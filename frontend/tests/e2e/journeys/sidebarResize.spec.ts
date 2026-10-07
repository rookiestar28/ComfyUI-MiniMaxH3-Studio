// M25-43 B-M2543-01: the H3 sidebar floor is a minimum, never a fixed width. Opened narrow, the
// host panel, its content wrapper and the H3 mount reach the 704 px floor; dragged wider, all three
// follow the panel; dragged narrower, the panel holds the floor; disposed, every inline value the
// controller wrote is gone. Measured from rendered boxes in a host-shaped flex splitter.
import { expect, test, type Page } from "@playwright/test";

test.use({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1 });

type Widths = Readonly<{
  panel: number;
  panelContent: number;
  content: number;
  mount: number;
  body: number;
  contentInlineWidth: string;
}>;

async function widths(page: Page): Promise<Widths> {
  return page.evaluate(() => {
    const width = (id: string) =>
      document.getElementById(id)!.getBoundingClientRect().width;
    const panel = document.getElementById("h3-width-panel")!;
    return {
      panel: width("h3-width-panel"),
      panelContent: panel.clientWidth,
      content: width("h3-width-content"),
      mount: width("h3-width-mount"),
      body: width("h3-width-body"),
      contentInlineWidth:
        document.getElementById("h3-width-content")!.style.width,
    };
  });
}

/** A gutter drag: PrimeVue rewrites the panel's flex-basis as a percentage of the splitter. */
async function drag(page: Page, basis: string): Promise<void> {
  await page.evaluate((value) => {
    document.getElementById("h3-width-panel")!.style.flexBasis = value;
  }, basis);
}

async function expectFollowing(
  page: Page,
  panelWidth: number,
  phase: string,
): Promise<void> {
  await expect
    .poll(async () => Math.round((await widths(page)).panel), {
      message: `${phase}: panel width`,
    })
    .toBe(panelWidth);
  // Give the controller's observer pass a frame to run, then measure the settled layout.
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  const measured = await widths(page);
  const diagnostic = `${phase}: ${JSON.stringify(measured)}`;
  expect(Math.round(measured.panel), diagnostic).toBe(panelWidth);
  expect(
    Math.abs(measured.content - measured.panelContent),
    diagnostic,
  ).toBeLessThanOrEqual(1);
  expect(
    Math.abs(measured.mount - measured.content),
    diagnostic,
  ).toBeLessThanOrEqual(1);
  expect(
    Math.abs(measured.body - measured.mount),
    diagnostic,
  ).toBeLessThanOrEqual(1);
}

test("the sidebar content follows its panel above the 704 px floor", async ({
  page,
}) => {
  await page.goto("/sidebarWidth.html");
  await expect(page.locator("body")).toHaveAttribute("data-ready", "true");

  await expectFollowing(page, 704, "opened narrow");

  // 62.5% of the 1600 px splitter less the gutter share: a 996 px panel.
  await drag(page, "calc(62.5% - 4px)");
  await expectFollowing(page, 996, "dragged wider");

  await drag(page, "calc(20% - 4px)");
  await expectFollowing(page, 704, "dragged below the floor");

  await drag(page, "calc(75% - 4px)");
  await expectFollowing(page, 1196, "dragged wider again");
  // H3 wrote no concrete width on the host's content wrapper: the host still sizes it.
  expect((await widths(page)).contentInlineWidth).toBe("");

  await page.evaluate(() =>
    (window as unknown as { h3DisposeWidth: () => void }).h3DisposeWidth(),
  );
  const released = await page.evaluate(() =>
    ["h3-width-panel", "h3-width-content", "h3-width-mount"].map((id) => {
      const style = document.getElementById(id)!.style;
      return { id, minWidth: style.minWidth, width: style.width };
    }),
  );
  expect(released).toEqual([
    { id: "h3-width-panel", minWidth: "", width: "" },
    { id: "h3-width-content", minWidth: "", width: "" },
    { id: "h3-width-mount", minWidth: "", width: "" },
  ]);
});
