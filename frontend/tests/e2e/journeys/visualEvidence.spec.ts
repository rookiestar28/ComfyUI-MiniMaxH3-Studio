import { expect, test } from "../fixtures/h3Page";

/**
 * M21-03 AC-12 and AC-17 — the evidence lane for the visual system.
 *
 * Everything here is measured in a real layout engine, because that is the only
 * place these claims can be true or false: a static rule can say a control is
 * 26px, but only a rendered box can say whether it overflowed the column at
 * 480px in Traditional Chinese.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/?mode=production");
});

const LOCALES = ["en", "zh-TW", "zh-CN"] as const;
/** The product floor and the declared narrow breakpoint. */
const WIDTHS = [704, 480] as const;

async function setSidebarWidth(
  page: import("@playwright/test").Page,
  width: number,
): Promise<void> {
  await page.setViewportSize({ width: width + 360, height: 1000 });
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

/** The page tab and the field are themselves localised, so both are matched
 *  across the closed locale set rather than by whichever name happens to be
 *  showing when the switch is made. */
const SETTINGS_TAB = /^(?:Settings|設定|设置)$/;
const LANGUAGE_FIELD = /^(?:Language|語言|语言)$/;
const PRODUCTION_TAB = /^(?:Production|導演台|导演台)$/;

async function setLocale(
  page: import("@playwright/test").Page,
  locale: string,
): Promise<void> {
  await page.getByRole("button", { name: SETTINGS_TAB }).click();
  await page.getByLabel(LANGUAGE_FIELD).selectOption(locale);
  // The write is asynchronous: the panel re-declares its language only once the
  // host has acknowledged it. Returning before that let a one-shot
  // `evaluate()` measure the previous locale's computed styles, which is
  // exactly the race the CJK variant test hit once the harness write acquired a
  // real delay. `lang` is the right thing to wait on rather than an arbitrary
  // pause, because it is the same signal the CJK variant itself keys on.
  await expect(page.locator("section.h3c")).toHaveAttribute("lang", locale);
}

test("the status band and the segment row hold in three locales at both widths", async ({
  page,
}) => {
  // AC-M21-03-17. Chinese renders the same content in noticeably fewer glyphs,
  // so both failure directions are checked: overflow in `en`, and a band or row
  // that has collapsed to nothing in `zh-*`.
  for (const locale of LOCALES) {
    await setLocale(page, locale);
    await page.getByRole("button", { name: PRODUCTION_TAB }).click();
    for (const width of WIDTHS) {
      await setSidebarWidth(page, width);
      const shell = page.locator(".h3c");
      const overflow = await shell.evaluate(
        (node) => node.scrollWidth - node.clientWidth,
      );
      expect(overflow, `${locale} @ ${width}`).toBeLessThanOrEqual(0);

      const band = page.locator(".h3p-i");
      const bandBox = await band.boundingBox();
      expect(bandBox, `${locale} @ ${width} band`).not.toBeNull();
      // Under-fill is a regression too: a band that has lost its content reads
      // as a broken header rather than as a quiet one.
      expect(bandBox!.width, `${locale} @ ${width} band width`).toBeGreaterThan(
        width * 0.6,
      );
      expect(
        bandBox!.height,
        `${locale} @ ${width} band height`,
      ).toBeGreaterThan(16);

      const row = page.locator(".h3p-s > li").first();
      const rowBox = await row.boundingBox();
      expect(rowBox, `${locale} @ ${width} row`).not.toBeNull();
      expect(
        rowBox!.width,
        `${locale} @ ${width} row width`,
      ).toBeLessThanOrEqual(width);

      const footer = page.locator(".h3p-f");
      await expect(footer, `${locale} @ ${width} footer`).toBeVisible();
    }
  }
  await setLocale(page, "en");
});

test("the CJK type variant activates and raises the floor it declares", async ({
  page,
}) => {
  // AC-M21-03-16. A variant nothing activates is a rule with no effect, so the
  // measurement is of rendered computed styles, not of the stylesheet.
  await page.getByRole("button", { name: PRODUCTION_TAB }).click();
  await setSidebarWidth(page, 704);
  const label = page.locator(".h3p-k").first();
  const latin = await label.evaluate((node) => {
    const style = getComputedStyle(node);
    return {
      size: Number.parseFloat(style.fontSize),
      transform: style.textTransform,
      tracking: style.letterSpacing,
    };
  });
  expect(latin.transform).toBe("uppercase");

  await setLocale(page, "zh-TW");
  await page.getByRole("button", { name: PRODUCTION_TAB }).click();
  await setSidebarWidth(page, 704);
  const cjk = await page
    .locator(".h3p-k")
    .first()
    .evaluate((node) => {
      const style = getComputedStyle(node);
      return {
        size: Number.parseFloat(style.fontSize),
        transform: style.textTransform,
        tracking: style.letterSpacing,
      };
    });
  // `uppercase` is a no-op on Han glyphs and `.11em` reads as broken spacing,
  // so both are dropped and the size floor rises to compensate.
  expect(cjk.transform).toBe("none");
  expect(["normal", "0px"]).toContain(cjk.tracking);
  expect(cjk.size).toBeGreaterThan(latin.size);
  expect(cjk.size).toBeGreaterThanOrEqual(11.5);
  await setLocale(page, "en");
});

test("controls clear the target size, depress, and stand down for the platform", async ({
  page,
}) => {
  // AC-M21-03-07 and AC-M21-03-12.
  await page.getByRole("button", { name: PRODUCTION_TAB }).click();
  await setSidebarWidth(page, 704);
  const boxes = await page.locator(".h3p button:visible").evaluateAll((nodes) =>
    nodes.map((node) => {
      const box = node.getBoundingClientRect();
      return {
        name: (node.textContent ?? "").trim().slice(0, 24),
        min: Math.min(box.width, box.height),
      };
    }),
  );
  expect(boxes.length).toBeGreaterThan(4);
  for (const box of boxes)
    expect(box.min, `${box.name} target size`).toBeGreaterThanOrEqual(24);

  // Under reduced motion nothing on a control animates. `:active` styles cannot
  // be read back from `getComputedStyle`, so the rule that removes the
  // depression is asserted statically in tests/m21_03VisualSystem.test.ts and
  // what is measured here is the part a browser can answer: the media query is
  // honoured and no transition survives it.
  await page.emulateMedia({ reducedMotion: "reduce" });
  const motion = await page
    .locator(".h3p button")
    .first()
    .evaluate((node) => ({
      honoured: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
      transition: getComputedStyle(node).transitionDuration,
    }));
  expect(motion.honoured).toBe(true);
  expect(
    motion.transition.split(",").every((value) => value.trim() === "0s"),
  ).toBe(true);
  await page.emulateMedia({ reducedMotion: null });

  // Forced colours: the spine keeps a visible shape and the prompt overlay
  // stands down so the field paints its own text again.
  await page.emulateMedia({ forcedColors: "active" });
  await expect(page.locator(".h3p-sp > i").first()).toBeVisible();
  await page.emulateMedia({ forcedColors: null });
});

test("shared controls expose state-correct cursor semantics", async ({
  page,
}) => {
  const root = page.locator("section.h3c");
  const contextTab = page.getByRole("button", { name: "Context", exact: true });
  await expect(contextTab).toHaveAttribute("aria-current", "page");
  const selectedBackground = await contextTab.evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  );
  await expect(contextTab).toHaveCSS("cursor", "pointer");
  await contextTab.hover();
  await expect(contextTab).toHaveCSS("cursor", "pointer");
  expect(
    await contextTab.evaluate((node) => getComputedStyle(node).backgroundColor),
  ).toBe(selectedBackground);

  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Language", { exact: true })).toHaveCSS(
    "cursor",
    "pointer",
  );

  // The ordinary harness has no stable product state with a disabled select.
  // These closed fixtures exercise the shipped cascade without pretending to
  // be component evidence; NoticeSurface behavior remains unit-tested.
  await root.evaluate((node) => {
    const fixture = document.createElement("div");
    fixture.id = "cursor-contract-fixture";
    fixture.innerHTML = `
      <button id="cursor-enabled" type="button" aria-selected="true">Enabled</button>
      <select id="cursor-enabled-select" aria-label="Enabled fixture select"><option>One</option></select>
      <button id="cursor-disabled" type="button" disabled>Disabled</button>
      <select id="cursor-disabled-select" aria-label="Disabled fixture select" disabled><option>One</option></select>
      <button id="cursor-plain" type="button" data-h3-plain>Plain</button>
      <span id="cursor-noninteractive">Text</span>
    `;
    fixture
      .querySelector("#cursor-plain")
      ?.addEventListener("click", (event) => {
        (event.currentTarget as HTMLElement).dataset.clicked = "true";
      });
    node.append(fixture);
  });

  for (const selector of ["#cursor-enabled", "#cursor-enabled-select"])
    await expect(page.locator(selector)).toHaveCSS("cursor", "pointer");

  for (const selector of ["#cursor-disabled", "#cursor-disabled-select"]) {
    await expect(page.locator(selector)).toBeDisabled();
    await expect(page.locator(selector)).toHaveCSS("cursor", "not-allowed");
  }

  const plain = page.locator("#cursor-plain");
  expect(
    await plain.evaluate((node) => getComputedStyle(node).cursor),
  ).not.toBe("pointer");
  await plain.click();
  await expect(plain).toHaveAttribute("data-clicked", "true");
  expect(
    await page
      .locator("#cursor-noninteractive")
      .evaluate((node) => getComputedStyle(node).cursor),
  ).not.toBe("pointer");
});

test("the sequence track selects, and the keyboard reaches every stage of it", async ({
  page,
}) => {
  // AC-M21-03-01 and AC-M21-03-12: navigation is real, and it is reachable
  // without a pointer.
  await page.getByRole("button", { name: PRODUCTION_TAB }).click();
  await setSidebarWidth(page, 704);
  const tile = page
    .getByRole("button", { name: "Select segment 2", exact: true })
    .first();
  await tile.focus();
  await expect(tile).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator(".h3p-s > li[data-edit-target]")).toHaveCount(1);
  await expect(
    page.locator(".h3p-s > li[data-edit-target] .h3p-ss"),
  ).toHaveCount(1);
  // Exactly one row is expanded, whatever the window shows.
  await expect(page.locator(".h3p-s > li .h3p-ss")).toHaveCount(1);
});

test("the backing plate is translucent while panels and controls keep their authority", async ({
  page,
}) => {
  await page.getByRole("button", { name: PRODUCTION_TAB }).click();
  await setSidebarWidth(page, 704);

  const alpha = async (selector: string): Promise<number> =>
    page
      .locator(selector)
      .first()
      .evaluate((node) => {
        const canvas = document.createElement("canvas");
        canvas.width = 1;
        canvas.height = 1;
        const context = canvas.getContext("2d");
        if (context === null) throw new Error("2D canvas unavailable");
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = getComputedStyle(node).backgroundColor;
        context.fillRect(0, 0, 1, 1);
        return context.getImageData(0, 0, 1, 1).data[3]! / 255;
      });

  const root = page.locator("section.h3c");
  for (const theme of ["dark", "light"] as const) {
    await root.evaluate((node, value) => {
      if (value === "light") node.setAttribute("data-theme", "light");
      else node.removeAttribute("data-theme");
    }, theme);

    expect(await alpha("section.h3c"), `${theme} plate`).toBeCloseTo(0.28, 2);
    expect(await alpha(".h3p-l > section"), `${theme} panel`).toBeLessThan(1);
    expect(await alpha(".h3p button"), `${theme} control`).toBe(1);
    await expect(page.locator(".h3p button").first()).toBeEnabled();
  }

  await root.evaluate((node) => node.classList.add("h3-reduced-transparency"));
  expect(await alpha("section.h3c"), "reduced-transparency plate").toBe(1);
  expect(await alpha(".h3p-l > section"), "reduced-transparency panel").toBe(1);
});
