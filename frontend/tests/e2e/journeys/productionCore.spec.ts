import { expect, test } from "../fixtures/h3Page";
import {
  installPinnedHostPalette,
  worstContrastWithin,
} from "../helpers/contrast";

test.beforeEach(async ({ page }) => {
  await page.goto("/?mode=production");
});

test("does not style unrelated host shorthand classes", async ({ page }) => {
  await page.evaluate(() => {
    const layout = document.createElement("div");
    layout.id = "unrelated-host-layout";
    layout.className = "hp-l hs-r";
    const copy = document.createElement("p");
    copy.id = "unrelated-host-copy";
    copy.className = "ht-a hd-s";
    document.body.append(layout, copy);
  });

  await expect(page.locator("#unrelated-host-layout")).toHaveCSS(
    "display",
    "block",
  );
  await expect(page.locator("#unrelated-host-copy")).toHaveCSS(
    "font-weight",
    "400",
  );
});

test("grid fills the owned sidebar on every top-level page", async ({
  page,
}) => {
  const owner = page.locator("#production-sidebar-container");
  await owner.evaluate((node) => {
    const element = node as HTMLElement;
    element.setAttribute("data-h3-context-mount", "");
    element.style.height = "700px";
    element.style.overflow = "auto";
    const sibling = document.createElement("div");
    sibling.id = "h3-grid-host-sibling";
    sibling.style.cssText = "display:block;height:37px;width:123px";
    element.parentElement?.append(sibling);
  });
  const nav = page.getByRole("navigation", { name: "H3 Context pages" });
  for (const name of ["Context", "Production", "Settings"]) {
    await nav.getByRole("button", { name }).click();
    const geometry = await owner.evaluate((node) => {
      const shell = node.querySelector<HTMLElement>(".h3c");
      if (shell === null) throw new Error("sidebar shell unavailable");
      const ownerRect = node.getBoundingClientRect();
      const shellRect = shell.getBoundingClientRect();
      return {
        bottomDelta: shellRect.bottom - ownerRect.bottom,
        horizontalOverflow: shell.scrollWidth - shell.clientWidth,
      };
    });
    expect(geometry.bottomDelta).toBeGreaterThanOrEqual(0);
    expect(geometry.horizontalOverflow).toBeLessThanOrEqual(0);
  }
  await nav.getByRole("button", { name: "Production" }).click();
  expect(
    await owner.evaluate((node) => node.scrollHeight > node.clientHeight),
  ).toBe(true);
  await expect(page.locator("#h3-grid-host-sibling")).toHaveCSS(
    "display",
    "block",
  );
  await expect(page.locator("#h3-grid-host-sibling")).toHaveCSS(
    "height",
    "37px",
  );
});

test("page navigation, settings, reorder and release use one acknowledged body", async ({
  page,
}) => {
  const nav = page.getByRole("navigation", { name: "H3 Context pages" });
  await expect(nav.getByRole("button")).toHaveText([
    "Context",
    "Production",
    "Settings",
  ]);
  await nav.getByRole("button", { name: "Production" }).click();
  for (const name of ["Segment", "Segment overview", "Run", "Outputs"])
    await expect(page.getByRole("region", { name, exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Select segment 2" }).click();
  await expect(page.locator("#production-last-action")).toHaveText(
    "set_selection",
  );
  await expect(page.locator("#production-action-count")).toHaveText("1");
  await page
    .getByRole("spinbutton", { name: "Move segment 2 to position" })
    .fill("1");
  await page
    .getByRole("button", { name: "Apply position for segment 2" })
    .click();
  await expect(page.locator("#production-last-action")).toHaveText(
    "reorder_segments",
  );
  await expect(page.locator("#production-action-count")).toHaveText("2");
  // The selection checkbox is labelled by its target, not by its own state.
  await expect(page.locator(".h3p-s > li").first()).toContainText("Segment 1");

  await nav.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByLabel("Language")).toHaveCount(1);
  await page.getByLabel("Language").selectOption("zh-TW");
  await expect(page.getByRole("button", { name: "導演台" })).toBeVisible();
  await expect(page.getByLabel("語言")).toHaveValue("zh-TW");
  await expect(page.getByRole("region", { name: "片段" })).toHaveCount(0);

  await page.getByRole("button", { name: "導演台" }).click();
  await page.getByRole("button", { name: "釋放工作區" }).click();
  await expect(page.getByRole("button", { name: "確認釋放" })).toBeVisible();
  await page.getByRole("button", { name: "確認釋放" }).click();
  await expect(page.locator(".h3n button").first()).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(page.getByRole("region", { name: "片段" })).toHaveCount(0);
  await expect(page.locator("#production-action-count")).toHaveText("3");
});

test("selection keeps a completed run authority and preview", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Show completed run" }).click();
  await expect(page.locator(".h3p-rs")).toHaveText("Succeeded");
  await expect(page.getByText("Sequence authority is unavailable")).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Preview aggregate output", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Preview segment 1" }),
  ).toHaveCount(2);
  await expect(
    page.getByRole("button", { name: "Preview segment 2" }),
  ).toHaveCount(2);
  await expect(
    page.getByRole("button", { name: "Preview segment 3" }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "Preview segment 1" }).first().click();
  await expect(page.getByLabel("Segment 1 preview")).toBeVisible();
  await expect(page.locator("#production-action-count")).toHaveText("0");

  await page.getByRole("button", { name: "Select segment 2" }).click();

  await expect(page.locator("#production-last-action")).toHaveText(
    "set_selection",
  );
  await expect(page.getByLabel("revision 5")).toBeVisible();
  await expect(page.locator(".h3p-rs")).toHaveText("Succeeded");
  await expect(page.getByText("Sequence authority is unavailable")).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Preview aggregate output", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Preview segment 2" }),
  ).toHaveCount(2);
  await page.getByRole("button", { name: "Preview segment 2" }).first().click();
  await expect(page.getByLabel("Segment 2 preview")).toBeVisible();
  await expect(page.locator("#production-action-count")).toHaveText("1");
  await page.getByRole("button", { name: "Close preview" }).click();
  await expect(page.getByText("Output 1", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Delete segment 2" }).click();

  await expect(page.locator("#production-last-action")).toHaveText(
    "delete_segment",
  );
  await expect(page.getByLabel("revision 6")).toBeVisible();
  await expect(
    page.getByText("Sequence authority is unavailable"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Preview aggregate output", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("Output 1", { exact: true })).toHaveCount(0);
});

test("clip preview openers reveal the player inside the owned sidebar viewport", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Show completed run" }).click();
  const owner = page.locator("#production-sidebar-container");
  await owner.evaluate((node) => {
    const element = node as HTMLElement;
    element.style.height = "420px";
    element.style.maxHeight = "420px";
    element.style.overflowY = "auto";
    element.scrollTop = 0;
  });

  const assertPlayerInsideOwner = async () => {
    await page.waitForFunction(
      () =>
        document.querySelector('video[aria-label="Segment 1 preview"]') !==
        null,
    );
    const player = page.getByLabel("Segment 1 preview");
    const geometry = await player.evaluate((node) => {
      const ownerNode = document.querySelector<HTMLElement>(
        "#production-sidebar-container",
      );
      if (ownerNode === null || !ownerNode.contains(node))
        return { contained: false, scrollTop: -1 };
      const target = node.getBoundingClientRect();
      const viewport = ownerNode.getBoundingClientRect();
      return {
        contained:
          target.top >= viewport.top &&
          target.bottom <= viewport.bottom &&
          target.left >= viewport.left &&
          target.right <= viewport.right,
        scrollTop: ownerNode.scrollTop,
      };
    });
    expect(geometry.contained).toBe(true);
    expect(geometry.scrollTop).toBeGreaterThan(0);
    await expect(page.locator("#h3p-preview-panel")).toBeFocused();
    await expect(page.locator("#production-action-count")).toHaveText("0");
  };

  const sequenceOpener = page
    .locator('section[aria-labelledby="h3p-sequence"]')
    .getByRole("button", { name: "Preview segment 1" });
  await sequenceOpener.focus();
  await page.keyboard.press("Enter");
  await assertPlayerInsideOwner();
  await page.getByRole("button", { name: "Close preview" }).click();
  await expect(sequenceOpener).toBeFocused();

  const segmentOpener = page
    .locator('section[aria-labelledby="h3p-segment"]')
    .getByRole("button", { name: "Preview segment 1" });
  await segmentOpener.scrollIntoViewIfNeeded();
  await segmentOpener.focus();
  const beforeSegmentOpen = await owner.evaluate(
    (node) => (node as HTMLElement).scrollTop,
  );
  await page.keyboard.press("Space");
  await assertPlayerInsideOwner();
  expect(
    await owner.evaluate((node) => (node as HTMLElement).scrollTop),
  ).toBeGreaterThan(beforeSegmentOpen);
  await page.getByRole("button", { name: "Close preview" }).click();
  await expect(segmentOpener).toBeFocused();
});

test("responsive, theme and accessibility fallbacks preserve structure", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Production" }).click();
  const sidebar = page.locator(".h3c");
  const pageButtons = page.locator(".h3n button");
  await expect(pageButtons).toHaveCount(3);
  const boxes = await pageButtons.evaluateAll((buttons) =>
    buttons.map((button) => {
      const box = button.getBoundingClientRect();
      return { width: box.width, height: box.height };
    }),
  );
  expect(new Set(boxes.map((box) => `${box.width}:${box.height}`)).size).toBe(
    1,
  );
  expect(boxes.every((box) => box.width >= 72 && box.height >= 36)).toBe(true);

  // M17-21: the sequence stays a proportional flex track at every width. The
  // narrow-mode fallback the M17-12 research asked for is the segment list,
  // which now carries duration, boundary, every lifecycle state and the same
  // actions; stacking the tiles as well only produced an empty-looking box.
  const cases = [
    { viewport: { width: 360, height: 800 }, container: 320, timeline: "flex" },
    { viewport: { width: 768, height: 900 }, container: 520, timeline: "flex" },
    {
      viewport: { width: 1024, height: 900 },
      container: 640,
      timeline: "flex",
    },
    {
      viewport: { width: 1280, height: 900 },
      container: 960,
      timeline: "flex",
    },
    {
      viewport: { width: 1440, height: 1000 },
      container: 1100,
      timeline: "flex",
    },
  ] as const;
  for (const item of cases) {
    await page.setViewportSize(item.viewport);
    await page
      .locator("#production-sidebar-container")
      .evaluate((node, width) => {
        (node as HTMLElement).style.width = `${width}px`;
        (node as HTMLElement).style.maxWidth = "none";
      }, item.container);
    await expect(page.locator("#production-sidebar-container")).toHaveCSS(
      "width",
      `${item.container}px`,
    );
    await expect(page.locator(".h3p-t")).toHaveCSS("display", item.timeline);
    const overflow = await sidebar.evaluate(
      (node) => node.scrollWidth - node.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  }

  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.evaluate(() =>
    document.documentElement.setAttribute("data-theme", "light"),
  );
  await expect(sidebar).toHaveCSS(
    "background-color",
    "color(srgb 0.92549 0.937255 0.956863 / 0.28)",
  );
  const transition = await page
    .getByRole("button", { name: "Production" })
    .evaluate((node) => getComputedStyle(node).transitionDuration);
  expect(transition.split(",").every((value) => value.trim() === "0s")).toBe(
    true,
  );
});

test("Production proposal review remains source-owned and responsive", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Production" }).click();
  const understand = page.getByRole("button", { name: "Understand segment" });
  await expect(understand).toBeEnabled();
  expect((await understand.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  await understand.click();
  await expect(page.getByText("Synthetic morning scene")).toBeVisible();
  await page.getByRole("button", { name: "Accept proposal" }).click();
  await expect(
    page.getByText("Proposal source added; Production is unchanged."),
  ).toBeVisible();
  await expect(page.locator("#production-action-count")).toHaveText("0");

  await page.setViewportSize({ width: 360, height: 800 });
  await page.locator("#production-sidebar-container").evaluate((node) => {
    (node as HTMLElement).style.width = "320px";
    (node as HTMLElement).style.maxWidth = "none";
  });
  await expect(page.locator(".h3p-pr")).toHaveCSS("display", "grid");
  const overflow = await page
    .locator(".h3c")
    .evaluate((node) => node.scrollWidth - node.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("bounded preview uses one player, twelve samples and exact teardown", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Show previewable" }).click();
  const opener = page.getByRole("button", { name: "Preview aggregate output" });
  await opener.click();

  const player = page.getByLabel("Aggregate output preview");
  await expect(player).toHaveAttribute("controls", "");
  await expect(player).toHaveAttribute("preload", "metadata");
  await expect(page.locator("video[autoplay]")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /seek preview to sample/i }),
  ).toHaveCount(12);
  await expect(
    page.getByRole("button", { name: "Boundary before segment 2" }),
  ).toBeVisible();
  expect((await opener.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  await page.locator(".h3p-vs").evaluate((node) => {
    const metrics = { times: [] as number[], draws: 0, clears: 0 };
    (
      window as unknown as {
        h3PreviewMetrics: typeof metrics;
      }
    ).h3PreviewMetrics = metrics;
    HTMLCanvasElement.prototype.getContext = (() => ({
      drawImage: () => {
        metrics.draws += 1;
      },
      clearRect: () => {
        metrics.clears += 1;
      },
    })) as unknown as typeof HTMLCanvasElement.prototype.getContext;
    let currentTime = 0;
    Object.defineProperty(node, "duration", { value: 12, configurable: true });
    Object.defineProperty(node, "currentTime", {
      configurable: true,
      get: () => currentTime,
      set: (value: number) => {
        currentTime = value;
        metrics.times.push(value);
        queueMicrotask(() => node.dispatchEvent(new Event("seeked")));
      },
    });
    node.dispatchEvent(new Event("loadedmetadata"));
  });
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              h3PreviewMetrics: {
                times: number[];
                draws: number;
                clears: number;
              };
            }
          ).h3PreviewMetrics,
      ),
    )
    .toMatchObject({ draws: 12, clears: 0 });
  const times = await page.evaluate(
    () =>
      (
        window as unknown as {
          h3PreviewMetrics: { times: number[] };
        }
      ).h3PreviewMetrics.times,
  );
  expect(times).toHaveLength(12);
  expect(times[0]).toBe(0);
  expect(times[11]).toBeCloseTo(11.999, 3);

  await player.evaluate((node) => {
    Object.defineProperty(node, "duration", { value: 12, configurable: true });
    Object.defineProperty(node, "currentTime", {
      value: 6.4,
      configurable: true,
    });
    node.dispatchEvent(new Event("timeupdate"));
  });
  await expect(
    page.getByRole("button", { name: /seek preview to sample/i }).nth(6),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("production-preview-playhead")).toHaveAttribute(
    "style",
    /left: 53\.333/,
  );

  const close = page.getByRole("button", { name: "Close preview" });
  expect((await close.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  await page.setViewportSize({ width: 360, height: 800 });
  await page.locator("#production-sidebar-container").evaluate((node) => {
    (node as HTMLElement).style.width = "320px";
    (node as HTMLElement).style.maxWidth = "none";
  });
  const pageOverflow = await page
    .locator(".h3c")
    .evaluate((node) => node.scrollWidth - node.clientWidth);
  expect(pageOverflow).toBeLessThanOrEqual(0);

  await close.click();
  await expect(player).toHaveCount(0);
  await expect(opener).toBeFocused();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              h3PreviewMetrics: { clears: number };
            }
          ).h3PreviewMetrics.clears,
      ),
    )
    .toBe(12);
});
