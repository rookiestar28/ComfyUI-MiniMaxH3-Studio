/**
 * M21-03 — the visual system, audited rather than eyeballed.
 *
 * The plan's section 3 values are a specification to implement, not a result.
 * Every colour pair is re-verified here against the surfaces it actually sits
 * on, in both palettes, and the CJK ramp is audited at its own sizes rather than
 * inheriting the Latin audit.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(process.cwd(), "src/styles/tokens.css"), "utf8");
/** Prose must never satisfy a guard: comments quote values they removed. */
const declarations = css.replace(/\/\*[\s\S]*?\*\//g, "");

const LIGHT_SELECTOR = '.h3c[data-theme="light"]';
const darkBlock = declarations.slice(0, declarations.indexOf(LIGHT_SELECTOR));
const lightBlock = declarations.slice(
  declarations.indexOf(LIGHT_SELECTOR),
  declarations.indexOf(".h3n {"),
);

function literal(block: string, name: string): string | undefined {
  return block.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`))?.[1];
}

/** A token's literal in one palette, falling back to the dark declaration. */
function token(block: string, name: string): string {
  const value = literal(block, name) ?? literal(darkBlock, name);
  if (value === undefined) throw new Error(`no literal for ${name}`);
  return value;
}

const linear = (channel: number) =>
  channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;

function luminance(hex: string): number {
  return channelLuminance(
    [1, 3, 5].map((index) =>
      linear(Number.parseInt(hex.slice(index, index + 2), 16) / 255),
    ),
  );
}

function channelLuminance([r, g, b]: readonly number[]): number {
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

function percentToken(block: string, name: string): number {
  const match =
    block.match(new RegExp(`${name}:\\s*([0-9]+(?:\\.[0-9]+)?)%\\s*;`)) ??
    darkBlock.match(new RegExp(`${name}:\\s*([0-9]+(?:\\.[0-9]+)?)%\\s*;`));
  if (match?.[1] === undefined) throw new Error(`no percentage for ${name}`);
  return Number.parseFloat(match[1]) / 100;
}

/** Exact sRGB alpha compositing over the black/white backdrop extrema. */
function worstCompositeContrast(
  foreground: string,
  surface: string,
  alpha: number,
): number {
  const foregroundLuminance = luminance(foreground);
  const surfaceChannels = [1, 3, 5].map(
    (index) => Number.parseInt(surface.slice(index, index + 2), 16) / 255,
  );
  const [low, high] = [0, 1].map((backdrop) =>
    channelLuminance(
      surfaceChannels.map((channel) =>
        linear(alpha * channel + (1 - alpha) * backdrop),
      ),
    ),
  );
  if (low <= foregroundLuminance && foregroundLuminance <= high) return 1;
  return Math.min(
    (Math.max(foregroundLuminance, low) + 0.05) /
      (Math.min(foregroundLuminance, low) + 0.05),
    (Math.max(foregroundLuminance, high) + 0.05) /
      (Math.min(foregroundLuminance, high) + 0.05),
  );
}

const PALETTES = [
  ["dark", darkBlock],
  ["light", lightBlock],
] as const;

/** The surfaces a foreground token can land on. */
const SURFACES = [
  "--h3-canvas",
  "--h3-surface-1",
  "--h3-surface-2",
  "--h3-surface-3",
  "--h3-well",
] as const;

/** Every foreground the section 3.3/3.4 ramp declares. */
const FOREGROUNDS = [
  "--h3-text-primary",
  "--h3-text-body",
  "--h3-text-muted",
  "--h3-text-dim",
  "--h3-tone-ok",
  "--h3-tone-info",
  "--h3-tone-warn",
  "--h3-tone-danger",
  "--h3-tone-idle",
  "--h3-page-outline-context",
  "--h3-page-outline-settings",
  // The Production identity is `var(--h3-signal-orange)`, so the literal behind
  // it is what the audit has to read.
  "--h3-signal-orange",
  "--h3-edit-target",
  "--h3-stage-intent",
  "--h3-stage-media",
  "--h3-stage-understand",
  "--h3-stage-audit",
  "--h3-stage-execute",
  "--dg",
] as const;

describe("M21-03 colour", () => {
  it("declares both palettes as literals the audit can read", () => {
    expect(darkBlock.length).toBeGreaterThan(0);
    expect(lightBlock.length).toBeGreaterThan(0);
    for (const [name, block] of PALETTES)
      for (const surface of SURFACES)
        expect(literal(block, surface), `${name} ${surface}`).toBeDefined();
  });

  it.each(PALETTES)(
    "keeps every declared foreground above AA on every surface (%s)",
    (name, block) => {
      for (const foreground of FOREGROUNDS) {
        const value = token(block, foreground);
        for (const surface of SURFACES) {
          const background = token(block, surface);
          expect(
            contrast(value, background),
            `${name}: ${foreground} ${value} on ${surface} ${background}`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    },
  );

  it.each(PALETTES)("widens the neutral ladder (%s)", (name, block) => {
    // The shipped dark ladder spanned a narrow band of near-neutral black, which
    // is why every boundary had to be drawn. A ladder whose steps are too close
    // cannot carry structure, so the separation is asserted, not assumed.
    const steps = [
      token(block, "--h3-canvas"),
      token(block, "--h3-surface-1"),
      token(block, "--h3-surface-2"),
    ].map(luminance);
    for (let index = 1; index < steps.length; index += 1)
      expect(
        Math.abs(steps[index]! - steps[index - 1]!),
        `${name} step ${index}`,
      ).toBeGreaterThan(0.004);
    // A well is recessed below the canvas in dark and raised above it in light,
    // but it is never the same value as the surface it sits in.
    expect(luminance(token(block, "--h3-well"))).not.toBe(
      luminance(token(block, "--h3-canvas")),
    );
  });

  it("reserves saturated colour for state and leaves headings unhued", () => {
    // AC-M21-03-04.
    expect(declarations).toMatch(
      /--h3-text-title-accent:\s*var\(--h3-text-primary\)/,
    );
    expect(declarations).not.toContain("--h3-grid-line");
    expect(declarations).not.toMatch(
      /background-image:\s*\n?\s*linear-gradient\([^)]*grid/,
    );
    expect(declarations).not.toMatch(/background-size:\s*24px 24px/);
  });

  it("gives each page exactly one identity hue, and danger its own", () => {
    for (const page of ["context", "production", "settings"])
      expect(declarations).toContain(`--h3-page-outline-${page}`);
    // The identity and the fault signal are different values: a failed control
    // must not read as "this is the Production page".
    expect(token(darkBlock, "--h3-tone-danger")).not.toBe(
      token(darkBlock, "--h3-signal-orange"),
    );
  });
});

describe("M21-12 backing plate translucency", () => {
  const PANEL_SURFACES = [
    "--h3-surface-1",
    "--h3-surface-2",
    "--h3-surface-3",
  ] as const;
  const PANEL_TEXT = [
    "--h3-text-primary",
    "--h3-text-body",
    "--h3-text-muted",
    "--h3-text-dim",
  ] as const;

  it.each(PALETTES)(
    "keeps the extension plate at 28% alpha (%s)",
    (name, block) => {
      expect(percentToken(block, "--h3-plate-alpha"), name).toBe(0.28);
      expect(declarations).toMatch(
        /background-color:\s*color-mix\(\s*in srgb,\s*var\(--h3-surface\) var\(--h3-plate-alpha\),\s*transparent\s*\)/s,
      );
    },
  );

  it.each(PALETTES)(
    "audits each text-panel pair against black and white backdrop extremes (%s)",
    (name, block) => {
      const alpha = percentToken(block, "--h3-panel-alpha");
      expect(alpha, `${name} panel alpha remains translucent`).toBeLessThan(1);
      for (const surface of PANEL_SURFACES)
        for (const foreground of PANEL_TEXT)
          expect(
            worstCompositeContrast(
              token(block, foreground),
              token(block, surface),
              alpha,
            ),
            `${name}: ${foreground} on translucent ${surface}`,
          ).toBeGreaterThanOrEqual(4.5);
    },
  );

  it("routes structural panels through named translucent fills", () => {
    for (const level of [1, 2, 3])
      expect(declarations).toMatch(
        new RegExp(
          `--h3-panel-${level}:\\s*color-mix\\(\\s*in srgb,\\s*var\\(--h3-surface-${level}\\) var\\(--h3-panel-alpha\\),\\s*transparent\\s*\\)`,
          "s",
        ),
      );
    expect(declarations).toMatch(
      /\.h3-context-header\s*\{[^}]*background:\s*var\(--h3-panel-1\)/s,
    );
    expect(declarations).toMatch(
      /\.h3p-l > section\s*\{[^}]*background:\s*var\(--h3-panel-1\)/s,
    );
    expect(declarations).toMatch(
      /\.h3p-s > li\s*\{[^}]*background:\s*var\(--h3-panel-2\)/s,
    );
    expect(declarations).toMatch(
      /\.h3-context-body,\s*\.h3p,\s*\.h3s-p,\s*\.h3a\s*\{[^}]*background:\s*var\(--h3-panel-1\)/s,
    );
  });

  it("keeps controls and fields on opaque materials", () => {
    expect(declarations).toMatch(
      /\.h3c :is\(button, select\):not\(\[data-h3-plain\]\)\s*\{[^}]*background-color:\s*var\(--h3-control\)/s,
    );
    expect(declarations).toMatch(
      /\.h3c :is\(input:not\(\[type="checkbox"\]\):not\(\[type="radio"\]\), textarea\)\s*\{[^}]*background:\s*var\(--h3-well\)/s,
    );
  });

  it("turns both alpha authorities opaque for reduced transparency", () => {
    expect(declarations).toMatch(
      /prefers-reduced-transparency:\s*reduce[\s\S]*--h3-plate-alpha:\s*100%[\s\S]*--h3-panel-alpha:\s*100%/,
    );
    expect(declarations).toMatch(
      /\.h3c\.h3-reduced-transparency\s*\{[^}]*--h3-plate-alpha:\s*100%[^}]*--h3-panel-alpha:\s*100%/s,
    );
  });
});

describe("M21-03 type", () => {
  it("declares the four sizes plus the quiet section label", () => {
    for (const name of [
      "--h3-t-title",
      "--h3-t-body",
      "--h3-t-val",
      "--h3-t-meta",
      "--h3-t-sec",
    ])
      expect(declarations).toContain(`${name}:`);
    expect(declarations).toMatch(/--h3-t-sec-track:\s*0\.11em/);
    expect(declarations).toMatch(/--h3-t-sec-transform:\s*uppercase/);
  });

  it("ranks a value above its label and drops an unknown value one step", () => {
    // The rule that does the most work in section 3.4.
    expect(declarations).toMatch(
      /\.h3-val\s*\{[^}]*color:\s*var\(--h3-text-primary\)/s,
    );
    expect(declarations).toMatch(
      /\.h3-sec\s*\{[^}]*color:\s*var\(--h3-text-muted\)/s,
    );
    expect(declarations).toMatch(
      /\[data-known="false"\][^{]*\{[^}]*color:\s*var\(--h3-text-dim\)/s,
    );
    for (const [name, block] of PALETTES) {
      const value = luminance(token(block, "--h3-text-primary"));
      const label = luminance(token(block, "--h3-text-muted"));
      const unknown = luminance(token(block, "--h3-text-dim"));
      const canvas = luminance(token(block, "--h3-canvas"));
      // "Higher step" means further from the surface, which inverts with the
      // palette; comparing distance rather than raw luminance says it once.
      expect(
        Math.abs(value - canvas),
        `${name} value outranks label`,
      ).toBeGreaterThan(Math.abs(label - canvas));
      expect(
        Math.abs(unknown - canvas),
        `${name} unknown recedes below label`,
      ).toBeLessThanOrEqual(Math.abs(label - canvas));
    }
  });
});

describe("M21-03 CJK variant", () => {
  const variant = declarations.slice(declarations.indexOf(":lang(zh)"));
  const block = variant.slice(0, variant.indexOf("}"));

  it("is declared for the locales the product actually ships", () => {
    expect(declarations).toContain(".h3c:lang(zh)");
    expect(declarations).toContain(".h3c :lang(zh)");
    // A variant nothing can activate is a control with no executed effect. The
    // panel root carries `lang`, which is what makes `:lang(zh)` reachable and
    // what a screen reader reads to choose a voice.
    const shell = readFileSync(
      join(process.cwd(), "src/components/H3Sidebar.tsx"),
      "utf8",
    );
    expect(shell).toMatch(/className="h3c"[\s\S]{0,400}?lang=\{locale\}/);
  });

  it("drops the transform and the tracking that do nothing to Han glyphs", () => {
    // AC-M21-03-16. `uppercase` is a no-op on Han, so the size cue would vanish
    // with nothing replacing it, and .11em tracking reads as broken spacing.
    expect(block).toMatch(/--h3-t-sec-track:\s*0\s*;/);
    expect(block).toMatch(/--h3-t-sec-transform:\s*none/);
  });

  it("raises every size floor and loosens the line height", () => {
    const sizes = (source: string, name: string) => {
      const value = source.match(new RegExp(`${name}:\\s*([^;]+);`))?.[1] ?? "";
      const match = value.match(/(\d+(?:\.\d+)?)px\/(\d+(?:\.\d+)?)/);
      expect(
        match,
        `${name} in ${source === block ? "cjk" : "latin"}`,
      ).not.toBeNull();
      return { size: Number(match![1]), height: Number(match![2]) };
    };
    for (const name of [
      "--h3-t-title",
      "--h3-t-body",
      "--h3-t-val",
      "--h3-t-meta",
      "--h3-t-sec",
    ]) {
      const latin = sizes(darkBlock, name);
      const cjk = sizes(block, name);
      expect(cjk.size, `${name} size floor`).toBeGreaterThanOrEqual(latin.size);
      expect(cjk.height, `${name} line height`).toBeGreaterThan(latin.height);
      // A declared floor, not a hopeful reuse: Han glyphs below 11.5px are not
      // comfortably legible where 9.5px Latin capitals are.
      expect(cjk.size, `${name} CJK minimum`).toBeGreaterThanOrEqual(11.5);
    }
  });

  it("audits its contrast at the CJK sizes rather than inheriting the Latin pass", () => {
    // Every CJK size is >= 11.5px, which is below the 18.66px WCAG large-text
    // threshold, so the 4.5 ratio applies to all of them -- the same audit the
    // Latin ramp took, re-affirmed here at the sizes Han text really renders at.
    for (const [name, palette] of PALETTES)
      for (const foreground of [
        "--h3-text-primary",
        "--h3-text-muted",
        "--h3-text-dim",
      ])
        expect(
          contrast(token(palette, foreground), token(palette, "--h3-canvas")),
          `${name} ${foreground} at CJK sizes`,
        ).toBeGreaterThanOrEqual(4.5);
  });
});

describe("M21-03 control material", () => {
  it("keeps exactly three button roles and never fills a destructive one", () => {
    expect(declarations).toMatch(/button\[data-variant="primary"\]/);
    expect(declarations).toMatch(/button\[data-variant="danger"\]/);
    expect(declarations).not.toContain('data-variant="quiet"');
    const danger = declarations.match(
      /\.h3c button\[data-variant="danger"\]:not\(\[data-h3-plain\]\)\s*\{([^}]*)\}/,
    )?.[1];
    expect(danger).toBeDefined();
    expect(danger).not.toMatch(/background:\s*var\(--h3-tone-danger\)/);
    expect(danger).toMatch(/color:\s*var\(--h3-tone-danger\)/);
  });

  it("clears the WCAG 2.2 target size and depresses on :active", () => {
    // AC-M21-03-07. 24px is the 2.5.8 minimum; the default sits above it.
    expect(declarations).toMatch(/--h3-control-height:\s*26px/);
    expect(declarations).toMatch(/--h3-control-height-sm:\s*24px/);
    expect(declarations).toMatch(
      /:active:not\(:disabled\)\s*\{[^}]*transform:\s*translateY\(1px\)/s,
    );
    expect(declarations).toMatch(
      /:active:not\(:disabled\)\s*\{[^}]*box-shadow:\s*var\(--h3-well-shadow\)/s,
    );
  });

  it("lights every raised face from one source above and recesses every field", () => {
    const raised = declarations.match(
      /\.h3c :is\(button, select\):not\(\[data-h3-plain\]\)\s*\{([^}]*)\}/,
    )?.[1];
    expect(raised).toBeDefined();
    expect(raised).toContain("linear-gradient(");
    expect(raised).toMatch(/border-block-end-color:\s*var\(--h3-line-strong\)/);
    expect(raised).toMatch(/inset 0 1px 0 var\(--h3-lift-top\)/);
    expect(raised).toContain("var(--h3-lift-shadow)");

    const field = declarations.match(
      /\.h3c :is\(input:not\(\[type="checkbox"\]\):not\(\[type="radio"\]\), textarea\)\s*\{([^}]*)\}/,
    )?.[1];
    expect(field).toBeDefined();
    expect(field).toMatch(/background:\s*var\(--h3-well\)/);
    expect(field).toMatch(/box-shadow:\s*var\(--h3-well-shadow\)/);
    expect(field).toContain("background-image: none");
  });

  it("stops the depression under prefers-reduced-motion", () => {
    expect(declarations).toMatch(
      /@media \(prefers-reduced-motion: reduce\)\s*\{[^}]*:active:not\(:disabled\)\s*\{\s*transform:\s*none;/s,
    );
  });
});

/**
 * M21-06 — selection and hover become visible, and stay auditable.
 *
 * The user asked for saturated selected fills, hover feedback on unselected
 * controls, and one rounded-rectangle shape for every control. Two things make
 * that more than a styling patch. The fills reuse hues the controls already own,
 * so every one of them is a text-on-colour pair the audit has to recompute
 * rather than assume; and the shared control material is declared *after* the
 * nav and tab rules and ties or beats them, so the previous selected states were
 * partially dead in the shipped sheet. These rows pin both.
 */

/** The shared hover rule, matched by shape: the formatter wraps its selector. */
const HOVER_RULE =
  /\.h3c\s+:is\(button, select\):not\(\[data-h3-plain\]\)[^{]*?:hover:not\(:disabled\)\s*\{/;

/** Every hue a control can be selected in. Fills reuse them; no hue is new. */
const SELECTED_FILLS = [
  "--h3-page-outline-context",
  "--h3-signal-orange",
  "--h3-page-outline-settings",
  "--h3-stage-intent",
  "--h3-stage-media",
  "--h3-stage-understand",
  "--h3-stage-audit",
  "--h3-stage-execute",
] as const;

describe("M21-06 selected fills are audited, not assumed", () => {
  it.each(PALETTES)(
    "reads its selected ink for the palette that defines it (%s)",
    (_name, block) => {
      expect(token(block, "--h3-signal-ink")).toMatch(/^#[0-9a-f]{6}$/i);
    },
  );

  it.each(PALETTES)(
    "clears 4.5:1 for the ink on every selected fill (%s)",
    (_name, block) => {
      const ink = token(block, "--h3-signal-ink");
      for (const hue of SELECTED_FILLS) {
        expect({
          hue,
          ratio: Number(contrast(ink, token(block, hue)).toFixed(2)),
        }).toEqual({ hue, ratio: expect.any(Number) });
        expect(contrast(ink, token(block, hue))).toBeGreaterThanOrEqual(4.5);
      }
    },
  );

  it.each(PALETTES)(
    "clears 3:1 for a selected fill against the control face it replaces (%s)",
    (_name, block) => {
      // WCAG 2.2 SC 1.4.11: the fill is the thing that says "selected", so it is
      // a non-text component state and must be distinguishable from the resting
      // control beside it.
      const control = token(block, "--h3-control");
      for (const hue of SELECTED_FILLS)
        expect(contrast(token(block, hue), control)).toBeGreaterThanOrEqual(3);
    },
  );
});

describe("M21-06 the state rules win the cascade they used to lose", () => {
  const material = declarations.indexOf(
    ".h3c :is(button, select):not([data-h3-plain]) {",
  );
  const selected = declarations.indexOf(
    '.h3c .h3n button[aria-current="page"],',
  );
  const hover = declarations.search(HOVER_RULE);

  it("declares the shared material and both state rules exactly once", () => {
    // Fail closed: an index of -1 would make every ordering check below pass
    // vacuously, which is the silence this suite exists to break.
    expect(material).toBeGreaterThan(-1);
    expect(selected).toBeGreaterThan(-1);
    expect(hover).toBeGreaterThan(-1);
  });

  it("declares selection and hover after the material that overrides them", () => {
    expect(selected).toBeGreaterThan(material);
    expect(hover).toBeGreaterThan(material);
  });

  it("leaves no earlier selected-state rule to be silently overridden", () => {
    // The old rules are re-homed, not duplicated: a second declaration before
    // the material is dead code that reads as if it were live.
    expect(declarations.slice(0, material)).not.toContain(
      '.h3n button[aria-current="page"] {',
    );
    expect(declarations.slice(0, material)).not.toContain(
      '.h3-stage-tab[aria-selected="true"] {',
    );
    expect(declarations.slice(0, material)).not.toContain(
      '.h3-app-mode-tabs button[aria-selected="true"] {',
    );
  });
});

describe("M21-06 shape and state layer", () => {
  it("declares one state-layer strength and reuses it", () => {
    expect(declarations).toMatch(/--h3-state-hover:\s*10%;/);
    expect(
      declarations.match(/var\(--h3-state-hover\)/g)?.length ?? 0,
    ).toBeGreaterThanOrEqual(2);
  });

  it("gives every interactive control the one control radius", () => {
    // The pill is what says "not a control", so it must survive on the badge
    // and disappear from the link that is one.
    const github = declarations.slice(
      declarations.indexOf(".h3-context-github {"),
    );
    expect(github.slice(0, github.indexOf("}"))).not.toContain("999px");
    expect(github.slice(0, github.indexOf("}"))).toContain(
      "border-radius: var(--h3-radius-control);",
    );
    expect(declarations).toMatch(
      /\.h3-app-stage-state \{[^}]*border-radius: 999px;/,
    );
  });

  it("moves a border with every hover tint so the state survives forced colours", () => {
    const block = declarations.slice(hoverRuleStart(), hoverRuleEnd());
    expect(block).toContain("background-color: color-mix(");
    expect(block).toContain("border-color: color-mix(");
  });

  it("keeps hover visible when forced colours discard the tint", () => {
    // Located by its selector, never as the sheet's last forced-colors block: later modules append
    // forced-colors blocks of their own (M25-41 did), and a positional lookup then reads theirs.
    const hover = declarations.indexOf(
      ".h3c :is(button, select):not([data-h3-plain]):hover:not(:disabled),",
    );
    expect(hover).toBeGreaterThan(0);
    const media = declarations.lastIndexOf(
      "@media (forced-colors: active)",
      hover,
    );
    expect(declarations.slice(media, hover).trim()).toBe(
      "@media (forced-colors: active) {",
    );
    expect(
      declarations.slice(hover, declarations.indexOf("}", hover)),
    ).toContain("border-color: Highlight;");
  });
});

function hoverRuleStart(): number {
  return declarations.search(HOVER_RULE);
}
function hoverRuleEnd(): number {
  return declarations.indexOf("}", hoverRuleStart());
}

describe("M21-06 hover never repaints a control the user already chose", () => {
  it("excludes the selected states in the selector rather than by specificity", () => {
    // Found in the browser lane, not by reading. `:hover:not(:disabled)` scores
    // the shared rule (0,4,1), which beats the (0,3,1) selected rule. A click
    // leaves the pointer on the control it just selected, so a selected tab was
    // repainted as a 10% tint at the moment it became selected -- and the
    // literal audit could not see it, because both colours were already legal.
    const selector = declarations.slice(
      hoverRuleStart(),
      declarations.indexOf("{", hoverRuleStart()),
    );
    // The formatter wraps a long selector, so both exclusions are matched by
    // shape rather than as literal substrings.
    expect(selector).toMatch(/:not\(\s*\[aria-current="page"\]\s*\)/);
    expect(selector).toMatch(/:not\(\s*\[aria-selected="true"\]\s*\)/);
  });
});

describe("shared-control cursor semantics", () => {
  it("qualifies the common material by native enabled and disabled state", () => {
    const enabled = declarations.match(
      /\.h3c :is\(button, select\):not\(\[data-h3-plain\]\):not\(:disabled\)\s*\{([^}]*)\}/,
    )?.[1];
    const disabled = declarations.match(
      /\.h3c :is\(button, select\):not\(\[data-h3-plain\]\):disabled\s*\{([^}]*)\}/,
    )?.[1];

    expect(enabled).toBeDefined();
    expect(enabled).toMatch(/cursor:\s*pointer/);
    expect(disabled).toBeDefined();
    expect(disabled).toMatch(/cursor:\s*not-allowed/);
  });

  it("keeps the plain opt-out and non-interactive descendants outside the rule", () => {
    const cursorSelectors = [
      ...declarations.matchAll(/([^{}]+)\{[^{}]*cursor:/g),
    ].map(([, selector]) => selector.trim());
    const shared = cursorSelectors.filter((selector) =>
      selector.includes(":is(button, select)"),
    );

    expect(shared).toHaveLength(2);
    for (const selector of shared) {
      expect(selector).toContain(":not([data-h3-plain])");
      expect(selector).not.toMatch(/(?:^|[,\s])(?:div|span|\*)\b/);
    }
  });
});
