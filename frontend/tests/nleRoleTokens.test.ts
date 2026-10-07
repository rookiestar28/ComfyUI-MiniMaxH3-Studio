/**
 * M25-61: the NLE role tokens are aliases of tokens.css, declared once on the overlay root in
 * nleWorkspace.css. M21-03 audits tokens.css literals only, so this audit resolves each role alias
 * against both palettes and checks the pairs the redesigned editor actually draws.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const stripComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, "");
const tokens = stripComments(
  readFileSync(join(process.cwd(), "src/styles/tokens.css"), "utf8"),
);
const workspace = stripComments(
  readFileSync(
    join(process.cwd(), "src/components/nle/nleWorkspace.css"),
    "utf8",
  ),
);

const LIGHT_SELECTOR = '.h3c[data-theme="light"]';
const darkBlock = tokens.slice(0, tokens.indexOf(LIGHT_SELECTOR));
const lightBlock = tokens.slice(
  tokens.indexOf(LIGHT_SELECTOR),
  tokens.indexOf(".h3n {"),
);
const PALETTES = [
  ["dark", darkBlock],
  ["light", lightBlock],
] as const;

const backdropStart = workspace.indexOf(".h3-nle-backdrop {");
const backdropBlock = workspace.slice(
  backdropStart,
  workspace.indexOf("}", backdropStart),
);

type Rgb = readonly [number, number, number];
/** A resolved colour: opaque sRGB, or an sRGB colour over an unknown backdrop at `alpha`. */
type Resolved = Readonly<{ rgb: Rgb; alpha: number }>;

function hex(value: string): Rgb {
  return [1, 3, 5].map(
    (index) => Number.parseInt(value.slice(index, index + 2), 16) / 255,
  ) as unknown as Rgb;
}

/** A token's literal in one palette, following `var()` aliases and falling back to dark. */
function literal(block: string, name: string, depth = 0): string {
  if (depth > 8) throw new Error(`alias cycle at ${name}`);
  const declared = (source: string) =>
    source.match(
      new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6}|var\\((--[a-z0-9-]+)\\))\\s*;`),
    );
  const match = declared(block) ?? declared(darkBlock);
  if (match?.[1] === undefined) throw new Error(`no literal for ${name}`);
  return match[2] === undefined
    ? match[1]
    : literal(block, match[2], depth + 1);
}

function roleSource(name: string): string {
  const match = backdropBlock.match(new RegExp(`${name}:\\s*([^;]+);`));
  if (match?.[1] === undefined) throw new Error(`no role token ${name}`);
  return match[1].replace(/\s+/g, " ").trim();
}

function reference(block: string, source: string): Rgb {
  const match = source.match(/^var\((--h3-[a-z0-9-]+)\)$/);
  if (match?.[1] === undefined) throw new Error(`not an alias: ${source}`);
  return hex(literal(block, match[1]));
}

/** Resolve a role token: an alias, or an sRGB colour-mix of an alias with an alias or transparency. */
function resolve(block: string, name: string): Resolved {
  const source = roleSource(name);
  if (source.startsWith("var("))
    return { rgb: reference(block, source), alpha: 1 };
  const mix = source.match(
    /^color-mix\( ?in srgb, (var\(--h3-[a-z0-9-]+\)) ([0-9.]+)%, (var\(--h3-[a-z0-9-]+\)|transparent) ?\)$/,
  );
  if (mix === null)
    throw new Error(`unsupported role value ${name}: ${source}`);
  const first = reference(block, mix[1]!);
  const share = Number.parseFloat(mix[2]!) / 100;
  if (mix[3] === "transparent") return { rgb: first, alpha: share };
  const second = reference(block, mix[3]!);
  return {
    rgb: first.map(
      (channel, index) => share * channel + (1 - share) * second[index]!,
    ) as unknown as Rgb,
    alpha: 1,
  };
}

const linear = (channel: number) =>
  channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
const luminance = (rgb: Rgb) =>
  0.2126 * linear(rgb[0]) + 0.7152 * linear(rgb[1]) + 0.0722 * linear(rgb[2]);
const ratio = (a: number, b: number) =>
  (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);

/** Worst contrast of an opaque foreground over a surface that may be translucent. */
function worstContrast(foreground: Resolved, surface: Resolved): number {
  if (foreground.alpha !== 1) throw new Error("foregrounds are opaque");
  const target = luminance(foreground.rgb);
  const backdrops =
    surface.alpha === 1
      ? [surface.rgb]
      : [[0, 0, 0] as const, [1, 1, 1] as const];
  return Math.min(
    ...backdrops.map((backdrop) =>
      ratio(
        target,
        luminance(
          surface.rgb.map(
            (channel, index) =>
              surface.alpha * channel + (1 - surface.alpha) * backdrop[index]!,
          ) as unknown as Rgb,
        ),
      ),
    ),
  );
}

const token = (block: string, name: string): Resolved =>
  name.startsWith("--h3-nle-")
    ? resolve(block, name)
    : { rgb: hex(literal(block, name)), alpha: 1 };

/** [foreground, surface, minimum ratio, what the editor draws]. */
const PAIRS = [
  [
    "--h3-text-primary",
    "--h3-nle-label-backing",
    4.5,
    "clip label over any filmstrip",
  ],
  ["--h3-text-dim", "--h3-well", 4.5, "ruler label on the ruler"],
  ["--h3-nle-playhead", "--h3-nle-track-band", 3, "playhead across a track"],
  ["--h3-nle-playhead", "--h3-well", 3, "playhead head in the ruler"],
  ["--h3-nle-selection", "--h3-nle-track-band", 3, "selected clip outline"],
  ["--h3-nle-waveform", "--h3-nle-track-band", 3, "waveform band"],
  ["--h3-nle-accent", "--h3-surface-1", 3, "active tab underline and toggles"],
  ["--h3-nle-text-clip", "--h3-nle-track-band", 3, "text clip edge"],
] as const;

describe("M25-61 NLE role tokens", () => {
  it("declares every role token as an alias of tokens.css, never a new literal", () => {
    const declared = [
      ...backdropBlock.matchAll(/(--h3-nle-[a-z-]+):\s*([^;]+);/g),
    ];
    expect(declared.length).toBeGreaterThanOrEqual(7);
    for (const [, name, value] of declared) {
      if (name === "--h3-nle-target" || name === "--h3-nle-ruler-height")
        continue;
      expect(value, name).not.toMatch(/#[0-9a-fA-F]{3,8}|\brgba?\(|\bhsla?\(/);
      for (const [palette, block] of PALETTES)
        expect(() => resolve(block, name!), `${palette} ${name}`).not.toThrow();
    }
  });

  it.each(PALETTES)(
    "keeps the pairs the editor draws above their WCAG floor (%s)",
    (_palette, block) => {
      for (const [foreground, surface, minimum, what] of PAIRS)
        expect(
          worstContrast(token(block, foreground), token(block, surface)),
          `${what}: ${foreground} on ${surface}`,
        ).toBeGreaterThanOrEqual(minimum);
    },
  );
});
