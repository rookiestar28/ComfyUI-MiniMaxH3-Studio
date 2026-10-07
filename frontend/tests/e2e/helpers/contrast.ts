import type { Page } from "@playwright/test";

/**
 * The WCAG sweep, extracted so more than one spec can run it.
 *
 * It was written for `M17-22`, where a light palette had shipped for three
 * items without ever rendering and none of its values had been measured. The
 * arithmetic is unchanged; only the root it sweeps is now a parameter, because
 * `M22-06` needs the same measurement over a surface that only exists after a
 * provider has been selected and consented to, and copying fifty lines of
 * colour maths into a second spec would let the two drift apart.
 */

export type ContrastReading = {
  ratio: number;
  text: string;
  color: string;
};

export function worstContrastWithin(
  page: Page,
  root: string,
): Promise<ContrastReading> {
  return page.evaluate((selector) => {
    const linear = (v: number) =>
      v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    // Chrome reports color-mix() results as `color(srgb r g b / a)` with 0-1
    // channels and rgb()/rgba() with 0-255 channels. Reading both on the same
    // scale silently reports light surfaces as near-black.
    type RGBA = [number, number, number, number];
    const parse = (value: string): RGBA => {
      const n = value.match(/[\d.]+(?:e[-+]?\d+)?/g)?.map(Number) ?? [];
      const scale = value.startsWith("color(") ? 1 : 255;
      return [
        (n[0] ?? 0) / scale,
        (n[1] ?? 0) / scale,
        (n[2] ?? 0) / scale,
        n[3] ?? 1,
      ];
    };
    const over = (fg: RGBA, bg: RGBA): RGBA => [
      fg[0] * fg[3] + bg[0] * (1 - fg[3]),
      fg[1] * fg[3] + bg[1] * (1 - fg[3]),
      fg[2] * fg[3] + bg[2] * (1 - fg[3]),
      1,
    ];
    const lum = (c: RGBA) =>
      0.2126 * linear(c[0]) + 0.7152 * linear(c[1]) + 0.0722 * linear(c[2]);
    let worst = { ratio: Infinity, text: "", color: "" };
    document.querySelectorAll(`${selector} *`).forEach((node) => {
      const text = (node.textContent ?? "").trim();
      if (text.length === 0 || node.children.length > 0) return;
      const style = getComputedStyle(node);
      if (style.visibility === "hidden" || style.display === "none") return;
      let background: RGBA = [1, 1, 1, 1];
      const chain: Element[] = [];
      for (let n: Element | null = node; n; n = n.parentElement) chain.push(n);
      for (const ancestor of chain.reverse()) {
        const layer = parse(getComputedStyle(ancestor).backgroundColor);
        if (layer[3] > 0) background = over(layer, background);
      }
      const a = lum(parse(style.color));
      const b = lum(background);
      const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      if (ratio < worst.ratio)
        worst = { ratio, text: text.slice(0, 40), color: style.color };
    });
    return worst;
  }, root);
}

/**
 * The pinned host's own values, installed before a sweep.
 *
 * Without them every `var(--host, literal)` in the panel resolves to its
 * literal and the sweep measures only the safe half of the pair -- the exact
 * hole through which `--dg` shipped at 3.886 while its fallback scored 5.641.
 */
export async function installPinnedHostPalette(page: Page): Promise<void> {
  await page.evaluate(() => {
    const root = document.documentElement;
    for (const [name, value] of [
      ["--error-text", "#ff4444"],
      ["--comfy-menu-bg", "rgba(24, 24, 24, 0.9)"],
      ["--comfy-menu-secondary-bg", "rgba(24, 24, 24, 0.9)"],
      ["--input-text", "#ddd"],
      ["--fg-color", "#fff"],
      ["--bg-color", "#09090b"],
      ["--content-bg", "#4e4e4e"],
      ["--border-color", "#29292c"],
    ])
      root.style.setProperty(name, value);
  });
}
