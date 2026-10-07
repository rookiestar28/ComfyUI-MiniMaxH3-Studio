import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(process.cwd(), "src/styles/tokens.css"), "utf8");
// Guards that scan for variable references must read declarations, not prose:
// the M17-22 corrective comment quotes the very `var(--error-text, ...)` it
// removed, and a raw-text scan would report the fix as the defect.
const declarations = css.replace(/\/\*[\s\S]*?\*\//g, "");

describe("M17-12 frozen industrial presentation", () => {
  it("contains the exact dark/light palette and pairwise page identities", () => {
    // M21-03 superseded the M17-12 neutral ladder and identity hues under the
    // user-accepted visual direction of 2026-08-18. The guard is unchanged in
    // kind -- an exact literal list plus the three page identities -- and pins
    // the new values; the contrast of every pair is audited separately in
    // tests/m21_03VisualSystem.test.ts.
    for (const literal of [
      "#0d0f13",
      "#eceff4",
      "#e7ebf0",
      "#252a31",
      "#ff6b35",
      "#b72a0d",
      "#6f8cff",
      "#3157b8",
      "#8b95a3",
      "#59616c",
    ])
      expect(css.toLowerCase()).toContain(literal);
    expect(css).toContain("--h3-page-outline-context");
    expect(css).toContain("--h3-page-outline-production");
    expect(css).toContain("--h3-page-outline-settings");
    // Section headings carry no hue (AC-M21-03-04): the accent name survives
    // for the identity guard above, but it resolves to the top text step.
    expect(css).toMatch(/--h3-text-title-accent:\s*var\(--h3-text-primary\)/);
  });

  it("freezes disjoint container boundaries and accessibility fallbacks", () => {
    // M21-03 AC-01 removed the >= 641px two-column page grid, so that boundary
    // is gone with it. The rest stay disjoint, and the absence is asserted
    // rather than left as a silently shorter list.
    for (const boundary of [
      "max-width: 480px",
      "min-width: 481px",
      "min-width: 560px",
    ])
      expect(css).toContain(boundary);
    expect(css).not.toContain("min-width: 641px");
    expect(css).not.toMatch(
      /\.h3p-l\s*\{[^}]*grid-template-columns:[^;]*\)\s+minmax/s,
    );
    expect(css).toContain("@media (forced-colors: active)");
    expect(css).toMatch(
      /\.h3n button\[aria-current="page"\][^{]*\{[^}]*color:\s*HighlightText;[^}]*background:\s*Highlight;/s,
    );
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
    expect(css).not.toContain("forced-color-adjust: none");
  });

  it("keeps Production proposal rows vertical, bounded and directly operable", () => {
    expect(css).toMatch(/\.h3p-pr\s*\{[^}]*display:\s*grid;[^}]*gap:\s*8px;/s);
    expect(css).toMatch(/\.h3p-pr\s*>\s*li\s*\{[^}]*min-width:\s*0;/s);
    expect(css).toMatch(/\.h3p-pr button[^}]*min-height:\s*44px;/s);
    expect(css).toMatch(/\.h3p-a button[^}]*min-height:\s*44px;/s);
  });
});

describe("M17-21 presentation redesign", () => {
  it("resolves every custom property it references", () => {
    const defined = new Set(
      [...css.matchAll(/(--[a-z0-9-]+)\s*:/g)].map((match) => match[1]),
    );
    const unresolved = [
      ...css.matchAll(/var\(\s*(--[a-z0-9-]+)\s*([,)])/g),
    ].filter((match) => match[2] === ")" && !defined.has(match[1]!));
    expect(unresolved.map((match) => match[1])).toEqual([]);
  });

  it("exempts checkbox and radio inputs from block-control sizing", () => {
    expect(css).toMatch(
      /\.h3p input:not\(\[type="checkbox"\]\):not\(\[type="radio"\]\)/,
    );
    expect(css).toMatch(
      /\.h3p input:is\(\[type="checkbox"\], \[type="radio"\]\)\s*\{[^}]*min-height:\s*0;/s,
    );
    expect(css).not.toMatch(/\.h3p button,\s*\.h3p select,\s*\.h3p input,/);
  });

  it("publishes one button variant vocabulary for every page", () => {
    // M21-03 reduced four treatments to three (AC-M21-03-06). `quiet` rendered
    // as a second kind of secondary beside the bare default, so it was removed
    // rather than redefined; the default now carries the secondary material.
    for (const variant of ["primary", "danger"])
      expect(css).toMatch(
        new RegExp(`\\.h3c button\\[data-variant="${variant}"\\]\\s*\\{`),
      );
    expect(css).not.toContain('data-variant="quiet"');
  });

  it("reserves the fault signal for destructive and failed affordances", () => {
    // M21-03 split the two jobs the one warm value used to hold. The page
    // identity stays on `--h3-signal-orange`; the danger *state* becomes its own
    // audited tone, so a failed affordance no longer reads as "this is the
    // Production page". The invariant M17-21 protected is unchanged and now
    // asserted directly: the fault signal appears nowhere but those two roles.
    expect(css).toMatch(/--h3-tone-danger:\s*#[0-9a-f]{6}/i);
    expect(css).not.toMatch(/--h3-tone-danger:\s*var\(--h3-signal-orange\)/);
    // Exactly one warm shell accent survives, and it stays on the navigation
    // identity plus destructive and failed affordances.
    expect(css).not.toContain("--h3-signal-amber");
    expect(css).toMatch(
      /--h3-page-outline-production:\s*var\(--h3-signal-orange\)/,
    );
    for (const selector of [
      /\.h3p-i\s*\{([^}]*)\}/,
      /\.h3p-s\s*>\s*li\[data-selected\]\s*\{([^}]*)\}/,
      /\.h3p-t\s*>\s*li\s*>\s*button\s*\{([^}]*)\}/,
      /\.h3p-t\s*>\s*li\s*>\s*button\[aria-pressed="true"\]\s*\{([^}]*)\}/,
    ]) {
      const body = css.match(selector)?.[1];
      expect(body).toBeDefined();
      expect(body).not.toContain("--h3-signal-orange");
    }
  });

  it("renders backend state as labelled, toned chips", () => {
    expect(css).toMatch(/\.h3p-k\s*\{[^}]*text-transform:\s*uppercase;/s);
    for (const tone of ["ok", "info", "warn", "danger"])
      expect(css).toMatch(
        new RegExp(`\\.h3p-c\\[data-tone="${tone}"\\]\\s*\\{`),
      );
    expect(css).toMatch(/\.h3p-kv\s*>\s*div\s*\{[^}]*display:\s*grid;/s);
    // M21-03 order 6 replaced the dashed empty box -- which mimicked a
    // disabled input -- with one quiet receding line, so the rule is gone
    // with its markup rather than left as unreachable CSS.
    expect(css).not.toContain(".h3p-e {");
    expect(css).toMatch(
      /@media \(forced-colors: active\)[\s\S]*\.h3p-c,[\s\S]*border:\s*1px solid ButtonText/,
    );
  });
});

describe("M17-22 host theme binding", () => {
  const lightSelector =
    ":root:not([data-theme]):not(.dark-theme) body.litegraph";
  const lightBlock = css.slice(
    css.indexOf(lightSelector),
    css.indexOf(".h3n {"),
  );

  it("activates the light palette on the host's own dark signal", () => {
    // ComfyUI 1.48.7 marks dark with `.dark-theme` on the root element and
    // treats bare `:root` as light; `body.litegraph` is the host marker that
    // keeps the offline/hermetic case on the accepted dark palette.
    expect(css).toContain(
      ":root:not([data-theme]):not(.dark-theme) body.litegraph .h3c:not([data-theme])",
    );
    // The explicit overrides stay available to harnesses and tests.
    for (const selector of [
      '.h3c[data-theme="light"]',
      ':root[data-theme="light"] .h3c',
      ".light-theme .h3c",
    ])
      expect(lightBlock).toContain(selector);
  });

  it("never derives the palette from a theme-invariant host variable", () => {
    // Measured on the pinned host: these are defined once on bare `:root` with
    // dark values and do not change with the theme, so binding the palette to
    // them would keep the panel dark in light mode.
    //
    // `--error-text` is on this list because of the M17-22 corrective, and it is
    // the reason the list is asserted at all rather than trusted: the first pass
    // of this test omitted it, `--dg` was still `var(--error-text, #ff8585)`, and
    // the panel rendered danger text in the host's #ff4444 at 3.886 on
    // --h3-control and 4.459 on --h3-surface-3 -- both under AA -- while every
    // test reported green. A guard list is only worth as much as its
    // completeness, so it is derived from the plan's measured table, not memory.
    for (const invariant of [
      "--comfy-menu-bg",
      "--comfy-menu-secondary-bg",
      "--input-text",
      "--fg-color",
      "--bg-color",
      "--content-bg",
      "--border-color",
      "--error-text",
    ])
      expect(declarations).not.toContain(`var(${invariant}`);
  });

  it("leaves exactly one inert host reference, and no rendered colour", () => {
    // `--h3-operation` keeps `var(--p-primary-color, #d5a24b)` because the
    // accepted M17-00 contract pins that shape. It is inert: nothing consumes
    // it, so it cannot reach a rendered colour. This asserts both halves, so
    // that adding a consumer forces the host-derivation question to be answered
    // deliberately rather than by inheritance.
    expect(css).toMatch(
      /--h3-operation:\s*var\(--p-primary-color,\s*#d5a24b\)/,
    );
    expect(declarations.match(/var\(--h3-operation/g)).toBeNull();

    // The whole-file sweep is the part that matters. The M17-22 guard list was
    // written from the plan's measured table and still missed `--error-text`,
    // which `--dg` was consuming a few lines below the code under audit. An
    // enumerated list can only catch names someone remembered; this catches any
    // host variable referenced but never defined here, including future ones.
    const referenced = new Set(
      [...declarations.matchAll(/var\((--[a-zA-Z0-9_-]+)/g)].map(
        (match) => match[1],
      ),
    );
    const defined = new Set(
      [...declarations.matchAll(/(--[a-zA-Z0-9_-]+)\s*:/g)].map(
        (match) => match[1],
      ),
    );
    expect([...referenced].filter((name) => !defined.has(name))).toEqual([
      "--p-primary-color",
    ]);
  });

  it("invents no host variable that the pinned revision does not define", () => {
    for (const absent of [
      "--error-color",
      "--text-muted",
      "--code-bg,",
      "--code-bg)",
    ])
      expect(css).not.toContain(`var(${absent}`);
  });

  it("overrides every accent the light palette would otherwise inherit", () => {
    // These were inherited from the dark block while the light palette was
    // dead code, and score 1.27-1.86 against the darkest light surface.
    for (const [token, value] of [
      ["--h3-stage-intent", "#6b38ff"],
      ["--h3-stage-media", "#006e8b"],
      ["--h3-stage-understand", "#845f00"],
      ["--h3-stage-audit", "#ca0a41"],
      ["--h3-stage-execute", "#177444"],
      ["--h3-tone-danger", "#a32307"],
      ["--dg", "#a32307"],
    ] as const)
      expect(lightBlock).toContain(`${token}: ${value};`);
  });

  it("keeps danger text above AA on the surfaces it actually sits on", () => {
    // This guard exists because neither of the two other contrast mechanisms
    // could see this token. The browser sweep composites real ancestors, but
    // every `--dg` consumer is an error or blocked state -- `.h3-context-error`,
    // `.h3-context-status--error`, `.h3-stage-tab--blocked small`,
    // `.h3-app-mode-source-blocker` -- that the Production harness never
    // renders, so the sweep walks past them. And the host-variable guard above
    // is a name list, which caught the derivation only once someone thought to
    // add the name. A value assertion needs neither.
    //
    // Two real failures were found this way. Dark `--dg` was
    // `var(--error-text, #ff8585)`, resolving to the host's #ff4444 at 3.886 on
    // --h3-control; light `--dg` was the identity literal #b72a0d at 4.466 on
    // the same surface. Both are now the audited danger role.
    const linear = (channel: number) =>
      channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
    const luminance = (hex: string) => {
      const [r, g, b] = [1, 3, 5].map((index) =>
        linear(Number.parseInt(hex.slice(index, index + 2), 16) / 255),
      );
      return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
    };
    const contrast = (a: string, b: string) => {
      const [x, y] = [luminance(a), luminance(b)];
      return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
    };
    const valueIn = (block: string, name: string) =>
      block.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`))?.[1];

    const darkBlock = declarations.slice(
      0,
      declarations.indexOf(lightSelector),
    );
    for (const [label, block] of [
      ["dark", darkBlock],
      ["light", lightBlock],
    ] as const) {
      const danger = valueIn(block, "--dg");
      expect(danger, `${label} --dg is defined as a literal`).toBeDefined();
      // `--h3-control` is the tightest of these: it backs `.h3-stage-tab`, whose
      // blocked `small` child is coloured with `--dg`.
      for (const surface of ["--h3-control", "--h3-surface-3", "--h3-canvas"]) {
        const background =
          valueIn(block, surface) ?? valueIn(darkBlock, surface);
        expect(background, `${label} ${surface}`).toBeDefined();
        expect(
          contrast(danger!, background!),
          `${label}: --dg ${danger} on ${surface} ${background}`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});

describe("M17-23 bounded segment presentation", () => {
  it("bounds the segment region's height and lets it scroll", () => {
    // Windowing bounds elements, not pixels. A card is 203.5px at the 704px
    // product floor, so even a 32-card page is about 6 800px of column and the
    // Run region would sit below all of it without an internal scroll.
    const list = css.match(/\.h3p-s\s*\{([^}]*)\}/)?.[1];
    expect(list).toBeDefined();
    expect(list).toMatch(/max-height:\s*min\(/);
    expect(list).toMatch(/overflow-y:\s*auto;/);
    expect(list).toMatch(/overscroll-behavior:\s*contain;/);
  });

  it("gives the single edit target its own accent, not the reserved signal", () => {
    // AC-M17-21-03 keeps exactly one warm accent for destructive and failed
    // affordances, and set selection already owns the blue. Borrowing either
    // for the edit target would either dilute the fault signal or make two
    // different meanings look the same, so the role gets its own token.
    expect(css).toMatch(/--h3-edit-target:\s*#a78bfa;/);
    expect(css).toMatch(/--h3-edit-target:\s*#5b21b6;/);
    const target = css.match(
      /\.h3p-s\s*>\s*li\[data-edit-target\]\s*\{([^}]*)\}/,
    )?.[1];
    expect(target).toBeDefined();
    expect(target).not.toContain("--h3-signal-orange");
    expect(target).not.toContain("--h3-page-outline-production");
    expect(target).not.toContain("--h3-page-outline-context");
    // The badge carries the meaning; the outline only reinforces it, so the
    // state is never signalled by colour alone.
    expect(css).toMatch(/\.h3p-et\s*\{/);
  });

  it("keeps the card grid two-track for every segment but the edit target", () => {
    // The third track exists only to seat the badge and collapses to zero
    // elsewhere, so the accepted M17-21 D7 geometry is unchanged.
    const card = css.match(/\.h3p-s\s*>\s*li\s*\{([^}]*)\}/)?.[1];
    expect(card).toMatch(
      /grid-template-columns:\s*minmax\(0,\s*1fr\)\s*auto\s*auto;/,
    );
    expect(card).toContain("container-name: h3-segment-card;");
  });
});
