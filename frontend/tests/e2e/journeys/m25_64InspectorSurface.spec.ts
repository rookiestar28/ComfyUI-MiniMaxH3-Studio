// M25-64 A64-3 and A64-6 in the browser: the inspector's Project settings and clip header, the
// rendered type and fields the canvas specifies (B-M2564-06), the region framing, and the
// timeline toolbar's unavailable state (B-M2564-07).
//
// The expected facts are the reference fixture's own output (`m25_20_semantic_corpus_base_v1.json`:
// 320 x 180, 24/1, AAC 48 kHz mono in MP4/H.264; the preload shape sets 144 frames), not values
// read back from the page.

import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  openIntegratedShell,
  shellSnapshot,
  shellSurface,
} from "../helpers/nleShell";

async function openReference(page: Page): Promise<Locator> {
  await openIntegratedShell(page, "reference-preload");
  return page.locator(shellSurface);
}

async function assemble(page: Page, overlay: Locator) {
  await page.evaluate(() =>
    window.nleShellHarness.applyTimelineCommands(
      window.nleShellHarness.referenceAssemblyCommands(),
    ),
  );
  await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(3);
}

test.use({ viewport: { width: 1600, height: 900 } });

test("A64-3: with no selection the inspector shows Project settings from the output, with no revision, counts or ids", async ({
  page,
}) => {
  const overlay = await openReference(page);
  const inspector = overlay.locator('[data-h3-nle-area="inspector"]');
  await expect(inspector.locator(".h3-nle-inspector-bar")).toHaveText(
    "Project",
  );
  const rows = await inspector
    .locator(".h3-nle-project-settings > div")
    .evaluateAll((items) =>
      items.map((item) => [
        item.querySelector("dt")!.textContent!.trim(),
        item.querySelector("dd")!.textContent!.trim(),
      ]),
    );
  expect(rows).toEqual([
    ["Resolution", "320 × 180"],
    ["Frame rate", "24 fps"],
    ["Duration", "00:00:06:00"],
    ["Audio", "AAC · 48 kHz · mono"],
    ["Export format", "MP4 · H.264"],
  ]);
  const text = (await inspector.textContent()) ?? "";
  expect(text).not.toMatch(/revision|accepted|track-|vid-|img-|clip-/iu);
  expect(text).not.toMatch(/\b\d+ (clips?|tracks?|assets?)\b/iu);
});

test("A64-3: a selected clip's header shows its name, range and duration", async ({
  page,
}) => {
  const overlay = await openReference(page);
  await assemble(page, overlay);
  await page
    .locator(
      '[data-h3-nle-clip="reference-primary"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  const header = overlay.locator(".h3-nle-clip-header");
  await expect(header.locator("[data-h3-nle-clip-name]")).toHaveText("Clip 01");
  // reference-primary: frames 0-48 of the 24/1 output.
  await expect(header.locator("[data-h3-nle-clip-range]")).toHaveText(
    "00:00:00:00 – 00:00:02:00 · 2.00 s",
  );
  await expect(header).not.toContainText("reference-primary");
});

test("B-M2564-06: the inspector's bar and value fields render the type and field the canvas specifies", async ({
  page,
}) => {
  const overlay = await openReference(page);
  const bar = await overlay
    .locator(".h3-nle-inspector-bar")
    .evaluate((element) => {
      const style = getComputedStyle(element);
      return [style.fontSize, style.fontWeight];
    });
  // Canvas: "Project" at 12 px / 600 (`Empty.dc.html`).
  expect(bar).toEqual(["12px", "600"]);
  await assemble(page, overlay);
  await page
    .locator(
      '[data-h3-nle-clip="reference-primary"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  const field = await overlay
    .locator('.h3-nle-value input[type="number"]')
    .first()
    .evaluate((element) => {
      const resolve = (token: string) => {
        const probe = document.createElement("span");
        probe.style.backgroundColor = `var(${token})`;
        element.parentElement!.append(probe);
        const value = getComputedStyle(probe).backgroundColor;
        probe.remove();
        return value;
      };
      // The field is the well: it draws the border and the radius, and the input inside it draws
      // the text. The same facts as before, each read from the element that now draws it.
      const style = getComputedStyle(element);
      const well = getComputedStyle(element.parentElement!);
      return {
        family: style.fontFamily,
        size: style.fontSize,
        border: well.borderTopColor,
        borderWidth: well.borderTopWidth,
        subtle: resolve("--h3-line-subtle"),
        radius: well.borderTopLeftRadius,
        shadow: [well.boxShadow, style.boxShadow],
        inputBorder: style.borderTopWidth,
      };
    });
  // Canvas: `font: 12px 'Cascadia Mono', …`, `border: 1px solid #242a33` (the subtle line), radius
  // 5 px, no inset shadow (`Main.dc.html`, the Scale field).
  expect(field.family).toMatch(/^"?Cascadia Mono"?,/u);
  expect(field.size).toBe("12px");
  expect(field.border).toBe(field.subtle);
  expect(field.borderWidth).toBe("1px");
  expect(field.inputBorder).toBe("0px");
  expect(field.radius).toBe("5px");
  expect(field.shadow).toEqual(["none", "none"]);
});

test("A64-6: the regions are rounded panels, and a gutter draws its line only on hover", async ({
  page,
}) => {
  const overlay = await openReference(page);
  const radii = await overlay
    .locator("[data-h3-nle-area]")
    .evaluateAll((areas) =>
      areas.map((area) => [
        area.getAttribute("data-h3-nle-area"),
        getComputedStyle(area).borderTopLeftRadius,
      ]),
    );
  expect(radii).toEqual([
    ["bin", "6px"],
    ["monitor", "6px"],
    ["inspector", "6px"],
    ["timeline", "6px"],
  ]);
  const splitter = overlay.locator(".h3-nle-splitter").first();
  const line = () =>
    splitter.evaluate(
      (element) => getComputedStyle(element, "::after").backgroundColor,
    );
  await page.mouse.move(0, 0);
  expect(await line()).toBe("rgba(0, 0, 0, 0)");
  await splitter.hover();
  expect(await line()).not.toBe("rgba(0, 0, 0, 0)");
});

test("B-M2564-07: an unavailable toolbar control reads as unavailable beside an available one", async ({
  page,
}) => {
  const overlay = await openReference(page);
  await page.mouse.move(0, 0);
  const tones = await overlay.evaluate((dialog) => {
    const channel = (v: number) =>
      v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    // Chrome reports a `color-mix()` result as `color(srgb r g b)` with 0-1 channels, and rgb()
    // with 0-255 channels (the same reading as `helpers/contrast.ts`).
    const luminance = (color: string) => {
      const scale = color.startsWith("color(") ? 1 : 255;
      const [r, g, b] = color
        .match(/[\d.]+(?:e[-+]?\d+)?/gu)!
        .map((value) => Number(value) / scale);
      return 0.2126 * channel(r!) + 0.7152 * channel(g!) + 0.0722 * channel(b!);
    };
    const tone = (control: string) => {
      const button = dialog.querySelector<HTMLElement>(
        `.h3-nle-timeline-toolbar [data-h3-nle-control="${control}"]`,
      )!;
      return {
        unavailable: button.getAttribute("aria-disabled") === "true",
        luminance: luminance(getComputedStyle(button).color),
      };
    };
    return { split: tone("clip.split"), snap: tone("transport.snap") };
  });
  // The empty timeline: Split is unavailable, Snap is available.
  expect(tones.split.unavailable).toBe(true);
  expect(tones.snap.unavailable).toBe(false);
  // Canvas: available `#d7dde6`, unavailable `#4d5868`, a luminance ratio of about 5.3. The dim
  // text colour alone gave about 1.9.
  const ratio = (tones.snap.luminance + 0.05) / (tones.split.luminance + 0.05);
  expect(ratio).toBeGreaterThanOrEqual(3);
});

// The clip inspector's Basic tab against the approved design (`Main.dc.html`, Properties): the
// control set and its order, the row and field geometry, the value format and the slider, measured
// on the rendered page. The design is drawn at an editor of 1600 x 900; the integrated shell's
// dialog is the viewport less 16 px on each side.

async function selectReferenceClip(page: Page): Promise<Locator> {
  const overlay = await openReference(page);
  await assemble(page, overlay);
  await page
    .locator(
      '[data-h3-nle-clip="reference-primary"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect(overlay.locator('[data-h3-nle-property="Scale"]')).toBeVisible();
  await page.mouse.move(0, 0);
  return overlay;
}

type Rgb = readonly [number, number, number];

/** A token's colour as the page resolves it. */
async function token(overlay: Locator, name: string): Promise<Rgb> {
  return overlay.evaluate((dialog, variable) => {
    const probe = document.createElement("span");
    probe.style.backgroundColor = `var(${variable})`;
    dialog.append(probe);
    const value = getComputedStyle(probe).backgroundColor;
    probe.remove();
    const channels = value
      .match(/[\d.]+/gu)!
      .slice(0, 3)
      .map(Number);
    return [channels[0]!, channels[1]!, channels[2]!] as const;
  }, name);
}

type SliderPixels = Readonly<{
  /** Rail thickness in CSS px, on a column clear of the thumb. */
  rail: number;
  /** Thumb diameter in CSS px, on the slider's middle row. */
  thumb: number;
  /** Centre of the thumb, as a fraction of the slider's width. */
  thumbAt: number;
  /** Accent-coloured rail left and right of the thumb, as fractions of the slider's width. */
  fillLeft: number;
  fillRight: number;
  /** Rail-coloured rail left and right of the thumb, as fractions of the slider's width. */
  railLeft: number;
  railRight: number;
}>;

/**
 * What a slider actually draws, read from its own screenshot: the rail and thumb are parts of the
 * native control, which the page cannot measure, so the picture is the only honest source.
 */
async function sliderPixels(
  page: Page,
  slider: Locator,
  colours: Readonly<{ rail: Rgb; fill: Rgb; thumb: Rgb }>,
): Promise<SliderPixels> {
  const png = (await slider.screenshot()).toString("base64");
  return page.evaluate(
    async ({ png, colours }) => {
      const blob = await (await fetch(`data:image/png;base64,${png}`)).blob();
      const bitmap = await createImageBitmap(blob);
      const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
      const context = canvas.getContext("2d")!;
      context.drawImage(bitmap, 0, 0);
      const { data, width, height } = context.getImageData(
        0,
        0,
        bitmap.width,
        bitmap.height,
      );
      const ratio = window.devicePixelRatio;
      // The panel behind a rail differs from the subtle rail colour by 14-18 per channel, so the
      // tolerance has to stay well under that or the whole column reads as rail.
      const TOLERANCE = 6;
      const kind = (x: number, y: number): "rail" | "fill" | "thumb" | null => {
        const at = (y * width + x) * 4;
        for (const name of ["thumb", "fill", "rail"] as const) {
          const colour = colours[name];
          if (
            Math.abs(data[at]! - colour[0]) <= TOLERANCE &&
            Math.abs(data[at + 1]! - colour[1]) <= TOLERANCE &&
            Math.abs(data[at + 2]! - colour[2]) <= TOLERANCE
          )
            return name;
        }
        return null;
      };
      const middle = Math.floor(height / 2);
      const row = Array.from({ length: width }, (_, x) => kind(x, middle));
      const thumbStart = row.indexOf("thumb");
      const thumbEnd = row.lastIndexOf("thumb");
      if (thumbStart < 0)
        throw new Error("no thumb on the slider's middle row");
      const count = (name: "rail" | "fill", from: number, to: number) =>
        row.slice(from, to).filter((value) => value === name).length / width;
      // A column of rail well clear of the thumb, on whichever side has more room.
      const clear =
        thumbStart > width - thumbEnd
          ? Math.floor(thumbStart / 2)
          : Math.floor((thumbEnd + width) / 2);
      let thickness = 0;
      for (let y = 0; y < height; y += 1) {
        const value = kind(clear, y);
        if (value === "rail" || value === "fill") thickness += 1;
      }
      return {
        rail: thickness / ratio,
        thumb: (thumbEnd - thumbStart + 1) / ratio,
        thumbAt: (thumbStart + thumbEnd + 1) / 2 / width,
        fillLeft: count("fill", 0, thumbStart),
        fillRight: count("fill", thumbEnd + 1, width),
        railLeft: count("rail", 0, thumbStart),
        railRight: count("rail", thumbEnd + 1, width),
      };
    },
    { png, colours },
  );
}

test.describe("the clip inspector's Basic tab follows the design", () => {
  test.use({ viewport: { width: 1632, height: 932 } });

  test("it offers the design's rows in the design's order, and nothing else at rest", async ({
    page,
  }) => {
    const overlay = await selectReferenceClip(page);
    const offered = await overlay
      .locator('[data-h3-nle-property-panel="basic"]')
      .evaluate((panel) => {
        const out: string[] = [];
        for (const node of panel.querySelectorAll<HTMLElement>(
          "section[aria-label], [data-h3-nle-property-pair], [data-h3-nle-property], [role='switch'], [data-h3-nle-disclosure], select, [data-h3-nle-control]",
        )) {
          if (node.closest("[hidden]") !== null) continue;
          if (node.tagName === "SECTION")
            out.push(`section:${node.getAttribute("aria-label")}`);
          else if (node.matches("[data-h3-nle-property-pair]"))
            out.push(`pair:${node.dataset.h3NlePropertyPair}`);
          else if (node.matches("[role='switch']"))
            out.push(
              `switch:${node.getAttribute("aria-label")}:${node.getAttribute("aria-checked")}`,
            );
          else if (node.matches("[data-h3-nle-disclosure]"))
            out.push(
              `more:${node.textContent}:${node.getAttribute("aria-expanded")}`,
            );
          else if (node.matches("select"))
            out.push(
              `select:${node.closest("label")!.querySelector("span")!.textContent}`,
            );
          else if (node.matches("[data-h3-nle-property]"))
            out.push(
              `${node.closest("[data-h3-nle-property-pair]") === null ? "row" : "field"}:${node.dataset.h3NleProperty}`,
            );
          else out.push(`control:${node.dataset.h3NleControl}`);
        }
        return out;
      });
    expect(offered).toEqual([
      "section:Transform",
      "row:Scale",
      "pair:Position",
      "field:Position X",
      "field:Position Y",
      "row:Rotation",
      "switch:Uniform scale:true",
      "more:Anchor and alignment:false",
      "section:Blend",
      "row:Opacity",
      "select:Blend",
    ]);
    // The closed group's controls and the commit buttons exist and are not shown.
    for (const control of [
      "transform.align.left",
      "visual.transform",
      "visual.opacity_blend",
    ])
      await expect(
        overlay.locator(`[data-h3-nle-control="${control}"]`),
      ).toBeHidden();
    await expect(
      page.getByRole("spinbutton", { name: "Anchor X (%)" }),
    ).toBeHidden();
    // Values in the design's format, each unit inside its field.
    for (const [name, value, unit] of [
      ["Scale (%)", "100", "%"],
      ["Position X (%)", "0.00", "%"],
      ["Position Y (%)", "0.00", "%"],
      ["Rotation (°)", "0.0", "°"],
      ["Opacity (%)", "100", "%"],
    ] as const) {
      const input = page.getByRole("spinbutton", { name, exact: true });
      await expect(input).toHaveValue(value);
      expect(
        await input.evaluate((element) => {
          const well = element.parentElement!;
          const unit = well.querySelector(".h3-nle-unit")!;
          const outer = well.getBoundingClientRect();
          const inner = unit.getBoundingClientRect();
          return [
            unit.textContent,
            inner.left >= outer.left && inner.right <= outer.right,
          ];
        }),
      ).toEqual([unit, true]);
    }
  });

  test("its rows, fields and switch have the design's geometry and tokens", async ({
    page,
  }) => {
    const overlay = await selectReferenceClip(page);
    const facts = await overlay.evaluate((dialog) => {
      const one = (root: Element, selector: string) =>
        root.querySelector<HTMLElement>(selector)!;
      const box = (element: Element) => {
        const rect = element.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          top: rect.top,
          width: rect.width,
          height: rect.height,
          middle: rect.top + rect.height / 2,
        };
      };
      const colour = (variable: string) => {
        const probe = document.createElement("span");
        probe.style.backgroundColor = `var(${variable})`;
        dialog.append(probe);
        const value = getComputedStyle(probe).backgroundColor;
        probe.remove();
        return value;
      };
      const row = (name: string) =>
        one(dialog, `.h3-nle-property-row[data-h3-nle-property="${name}"]`);
      const scale = row("Scale");
      const rotation = row("Rotation");
      const well = one(scale, ".h3-nle-value");
      const input = one(well, "input");
      const pair = one(dialog, "[data-h3-nle-property-pair]");
      const wells = [...pair.querySelectorAll<HTMLElement>(".h3-nle-value")];
      const toggle = one(dialog, '[role="switch"]');
      const track = getComputedStyle(toggle, "::before");
      const knob = getComputedStyle(toggle, "::after");
      const transform = one(dialog, 'section[aria-label="Transform"]');
      const title = one(transform, ".h3-nle-property-disclosure");
      const wellStyle = getComputedStyle(well);
      const inputStyle = getComputedStyle(input);
      return {
        label: box(one(scale, "label")),
        range: box(one(scale, 'input[type="range"]')),
        well: box(well),
        nextRowTop: box(pair).top,
        scaleTop: box(scale).top,
        rotationWell: box(one(rotation, ".h3-nle-value")),
        wellStyle: {
          borderWidth: wellStyle.borderTopWidth,
          border: wellStyle.borderTopColor,
          radius: wellStyle.borderTopLeftRadius,
          ground: wellStyle.backgroundColor,
        },
        inputStyle: {
          family: inputStyle.fontFamily,
          size: inputStyle.fontSize,
          align: inputStyle.textAlign,
        },
        pairLabel: box(one(pair, ".h3-nle-property-label")),
        pairWells: wells.map(box),
        prefixes: wells.map(
          (member) => one(member, ".h3-nle-prefix").textContent,
        ),
        pairRanges: pair.querySelectorAll('input[type="range"]').length,
        toggle: box(toggle),
        track: [track.width, track.height, track.backgroundColor],
        knob: [knob.width, knob.height],
        title: [
          getComputedStyle(title).fontSize,
          getComputedStyle(title).fontWeight,
        ],
        chevron: getComputedStyle(one(title, ".h3-nle-property-chevron"))
          .opacity,
        reset: box(one(transform, ".h3-nle-property-reset")),
        tokens: {
          subtle: colour("--h3-line-subtle"),
          ground: colour("--h3-well"),
          accent: colour("--h3-nle-accent"),
        },
      };
    });
    // A slider row: label 76, slider, value 86 on one line; the design's 36 px row pitch.
    expect(facts.label.width).toBeCloseTo(76, 0);
    expect(facts.well.width).toBeCloseTo(86, 0);
    expect(Math.abs(facts.label.middle - facts.range.middle)).toBeLessThan(1.5);
    expect(Math.abs(facts.well.middle - facts.range.middle)).toBeLessThan(1.5);
    expect(facts.range.left).toBeGreaterThanOrEqual(facts.label.right + 9);
    expect(facts.well.left).toBeGreaterThanOrEqual(facts.range.right + 9);
    expect(facts.nextRowTop - facts.scaleTop).toBeCloseTo(36, 0);
    // The field: a well with the subtle 1 px border and a 5 px radius, 12 px mono, right aligned.
    expect(facts.wellStyle).toEqual({
      borderWidth: "1px",
      border: facts.tokens.subtle,
      radius: "5px",
      ground: facts.tokens.ground,
    });
    expect(facts.inputStyle.family).toMatch(/^"?Cascadia Mono"?,/u);
    expect(facts.inputStyle.size).toBe("12px");
    expect(facts.inputStyle.align).toBe("right");
    // Position: the label and both fields on one line, each field with its axis letter.
    expect(facts.pairRanges).toBe(0);
    expect(facts.prefixes).toEqual(["X", "Y"]);
    expect(facts.pairWells).toHaveLength(2);
    for (const member of facts.pairWells)
      expect(Math.abs(member.middle - facts.pairLabel.middle)).toBeLessThan(
        1.5,
      );
    expect(facts.pairWells[0]!.left).toBeCloseTo(facts.range.left, 0);
    expect(facts.pairWells[1]!.right).toBeCloseTo(facts.well.right, 0);
    expect(
      Math.abs(facts.pairWells[0]!.width - facts.pairWells[1]!.width),
    ).toBeLessThan(1);
    // Uniform scale: the design's 34 x 18 switch with a 14 px knob, at the row's end, on.
    expect(facts.track).toEqual(["34px", "18px", facts.tokens.accent]);
    expect(facts.knob).toEqual(["14px", "14px"]);
    expect(facts.toggle.right).toBeCloseTo(facts.well.right, 0);
    // The section head: a 12 px / 600 title with no chevron at rest, the reset at the right.
    expect(facts.title).toEqual(["12px", "600"]);
    expect(facts.chevron).toBe("0");
    expect(facts.reset.right).toBeCloseTo(facts.well.right, 0);
  });

  for (const ratio of [1, 1.25, 1.5] as const)
    test.describe(`at a device pixel ratio of ${ratio}`, () => {
      test.use({ deviceScaleFactor: ratio });
      test("its sliders draw the design's rail, fill and thumb", async ({
        page,
      }) => {
        const overlay = await selectReferenceClip(page);
        const colours = {
          rail: await token(overlay, "--h3-line-subtle"),
          fill: await token(overlay, "--h3-nle-accent"),
          thumb: await token(overlay, "--h3-text-primary"),
        };
        const measure = (name: string) =>
          sliderPixels(
            page,
            page.getByRole("slider", { name, exact: true }),
            colours,
          );
        // A measured run is the pixels drawn fully in the part's colour. An edge that falls
        // between device pixels is blended and not counted, so a part of n CSS px reads as n less
        // at most one device pixel per side for the round thumb, and within one for the rail.
        const thumbOf = (size: number, measured: number) => {
          expect(measured).toBeGreaterThanOrEqual(size - 2 / ratio - 0.01);
          expect(measured).toBeLessThanOrEqual(size + 1 / ratio + 0.01);
        };
        const railOf = (size: number, measured: number) => {
          expect(measured).toBeGreaterThanOrEqual(size - 1 / ratio - 0.01);
          expect(measured).toBeLessThanOrEqual(size + 1 / ratio + 0.01);
        };
        // Scale at 100 % of its 0.01-200 % slider: the thumb at the middle, filled up to it.
        const scale = await measure("Scale (%) slider");
        railOf(4, scale.rail);
        thumbOf(12, scale.thumb);
        expect(scale.thumbAt).toBeCloseTo(0.5, 1);
        expect(scale.fillLeft).toBeGreaterThan(0.4);
        expect(scale.railLeft).toBeLessThan(0.02);
        expect(scale.fillRight).toBeLessThan(0.02);
        expect(scale.railRight).toBeGreaterThan(0.4);
        // Rotation at 0: the thumb at the middle and no fill on either side.
        const rotation = await measure("Rotation (°) slider");
        expect(rotation.thumbAt).toBeCloseTo(0.5, 1);
        expect(rotation.fillLeft + rotation.fillRight).toBeLessThan(0.02);
        expect(rotation.railLeft).toBeGreaterThan(0.4);
        expect(rotation.railRight).toBeGreaterThan(0.4);
        // Opacity at 100 %: the thumb at the end, filled whole.
        const opacity = await measure("Opacity (%) slider");
        expect(opacity.thumbAt).toBeGreaterThan(0.9);
        expect(opacity.fillLeft).toBeGreaterThan(0.85);
        expect(opacity.railLeft).toBeLessThan(0.02);
        // The timeline's zoom slider: the design's 3 px rail in the strong line, a 10 px thumb,
        // no fill.
        const zoom = await sliderPixels(
          page,
          overlay.locator('[data-h3-nle-control="transport.zoom_continuous"]'),
          { ...colours, rail: await token(overlay, "--h3-line-strong") },
        );
        railOf(3, zoom.rail);
        thumbOf(10, zoom.thumb);
        expect(zoom.fillLeft + zoom.fillRight).toBeLessThan(0.02);
        expect(zoom.railLeft + zoom.railRight).toBeGreaterThan(0.6);
      });
    });

  test("the switch, the secondary group and a slider drag each do what they show", async ({
    page,
  }) => {
    const overlay = await selectReferenceClip(page);
    const receipts = async () => (await shellSnapshot(page)).receipts;
    const accepted = async () =>
      (await shellSnapshot(page)).timelineSnapshot!.clips.find(
        (clip) => clip.clipId === "reference-primary",
      )!.transform;
    const before = await receipts();
    const toggle = page.getByRole("switch", { name: "Uniform scale" });
    // Off: two independent fields, nothing sent.
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    const scaleX = page.getByRole("spinbutton", { name: "Scale X (%)" });
    await expect(scaleX).toBeVisible();
    await scaleX.fill("150");
    await expect(
      page.getByRole("spinbutton", { name: "Scale Y (%)" }),
    ).toHaveValue("100");
    await expect(
      overlay.locator('[data-h3-nle-control="visual.transform"]'),
    ).toBeVisible();
    expect(await receipts()).toBe(before);
    // On again: Y takes X as one accepted edit, and the single Scale row returns.
    await toggle.click();
    await expect.poll(receipts).toBe(before + 1);
    expect(await accepted()).toMatchObject({
      scale_x_bp: 15_000,
      scale_y_bp: 15_000,
    });
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(
      page.getByRole("spinbutton", { name: "Scale (%)", exact: true }),
    ).toHaveValue("150");
    await expect(
      overlay.locator('[data-h3-nle-control="visual.transform"]'),
    ).toBeHidden();
    // The secondary group opens to Anchor and the six alignment buttons.
    const more = overlay.locator(
      '[data-h3-nle-disclosure="transform.anchor_align"]',
    );
    await more.click();
    await expect(more).toHaveAttribute("aria-expanded", "true");
    await expect(
      page.getByRole("spinbutton", { name: "Anchor X (%)" }),
    ).toHaveValue("50");
    await expect(
      overlay.locator('[data-h3-nle-control^="transform.align."]:visible'),
    ).toHaveCount(6);
    // A drag on the Rotation slider is one accepted edit at its release.
    const slider = page.getByRole("slider", { name: "Rotation (°) slider" });
    const box = (await slider.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.75, box.y + box.height / 2, {
      steps: 6,
    });
    expect(await receipts()).toBe(before + 1);
    await page.mouse.up();
    await expect.poll(receipts).toBe(before + 2);
    const rotation = (await accepted()).rotation_mdeg as number;
    expect(rotation).toBeGreaterThan(60_000);
    expect(rotation).toBeLessThan(120_000);
  });
});

for (const [width, height, wraps] of [
  [752, 512, true],
  [1312, 752, false],
  [1632, 932, false],
  [1952, 1112, false],
] as const)
  test(`the inspector's rows hold at an editor of ${width - 32} x ${height - 32} with the widest values`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height });
    const overlay = await selectReferenceClip(page);
    // The widest text each field admits; typed, not committed.
    for (const [name, text] of [
      ["Scale (%)", "799.99"],
      ["Position X (%)", "-400.00"],
      ["Position Y (%)", "-399.99"],
      ["Rotation (°)", "-179.999"],
      ["Opacity (%)", "99.99"],
    ] as const)
      await page.getByRole("spinbutton", { name, exact: true }).fill(text);
    await page.mouse.move(0, 0);
    const facts = await overlay
      .locator('[data-h3-nle-area="inspector"]')
      .evaluate((area) => {
        const panel = area.querySelector<HTMLElement>(
          '[data-h3-nle-property-panel="basic"]',
        )!;
        const pair = panel.querySelector<HTMLElement>(
          "[data-h3-nle-property-pair]",
        )!;
        const label = pair
          .querySelector(".h3-nle-property-label")!
          .getBoundingClientRect();
        const wells = [
          ...pair.querySelectorAll<HTMLElement>(".h3-nle-value"),
        ].map((well) => well.getBoundingClientRect());
        return {
          overflowX: area.scrollWidth - area.clientWidth,
          clipped: [
            ...panel.querySelectorAll<HTMLInputElement>(
              '.h3-nle-value input[type="number"]',
            ),
          ]
            .filter((input) => input.closest("[hidden]") === null)
            .filter((input) => input.scrollWidth > input.clientWidth)
            .map((input) => input.getAttribute("aria-label")),
          rowsWider: [
            ...panel.querySelectorAll<HTMLElement>(
              ".h3-nle-property-row, .h3-nle-property-pair, .h3-nle-property-toggle",
            ),
          ]
            .filter((row) => row.closest("[hidden]") === null)
            .filter((row) => row.scrollWidth > row.clientWidth + 1).length,
          wrapped: wells[0]!.top >= label.bottom - 1,
          sameLine:
            Math.abs(
              wells[0]!.top +
                wells[0]!.height / 2 -
                (label.top + label.height / 2),
            ) < 1.5,
          wellWidths: wells.map((well) => Math.round(well.width)),
        };
      });
    expect(facts.overflowX).toBe(0);
    expect(facts.clipped).toEqual([]);
    expect(facts.rowsWider).toBe(0);
    // Position sits beside its label wherever both fields fit there, and under it only below.
    expect(facts.wrapped).toBe(wraps);
    expect(facts.sameLine).toBe(!wraps);
    for (const member of facts.wellWidths)
      expect(member).toBeGreaterThanOrEqual(87);
  });
