// M25-61 (redesign foundation): the glyph set the redesigned editor draws from, the editor's
// three-locale copy tables, and the control-target contract the stylesheet mirrors.

import { cleanup, render } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import {
  NLE_ICON_NAMES,
  NleActionIcon,
} from "../src/components/nle/NleIconActions";
import { copy } from "../src/components/nle/nleCopy";
import { NLE_REFERENCE_UI_CONTRACT_V1 } from "../src/contracts/nleReferenceUiContract";
import { SUPPORTED_LOCALES } from "../src/i18n/catalog";

afterEach(cleanup);

describe("redesign glyphs", () => {
  it("draws every glyph, including the redesign additions", () => {
    for (const name of [
      "close",
      "check",
      "warning",
      "search",
      "sortFilter",
      "grid",
      "list",
      "lock",
      "unlock",
      "eye",
      "eyeOff",
      "trackVideo",
      "trackImage",
      "text",
      "cornerGrip",
    ] as const)
      expect(NLE_ICON_NAMES).toContain(name);
    for (const name of NLE_ICON_NAMES) {
      const { container, unmount } = render(<NleActionIcon name={name} />);
      const paths = [...container.querySelectorAll("svg path")];
      expect(paths.length, name).toBeGreaterThan(0);
      for (const path of paths)
        expect(
          path.getAttribute("d")?.trim().length ?? 0,
          name,
        ).toBeGreaterThan(2);
      unmount();
    }
  });
});

type Shape = string | { readonly [key: string]: Shape };

/** The key tree of a copy table: leaves are value kinds, so a missing or retyped key differs. */
function shape(value: unknown): Shape {
  if (value === null || typeof value !== "object") return typeof value;
  return Object.fromEntries(
    Object.keys(value as object)
      .sort()
      .map((key) => [key, shape((value as Record<string, unknown>)[key])]),
  );
}

describe("editor copy parity", () => {
  it("declares the same keys with the same kinds in every locale", () => {
    const english = shape(copy.en);
    for (const locale of SUPPORTED_LOCALES)
      expect(shape(copy[locale]), locale).toEqual(english);
  });
});

describe("control-target contract", () => {
  it("states the fine, coarse and minimum targets the stylesheet mirrors", () => {
    const targets = NLE_REFERENCE_UI_CONTRACT_V1.controlTargetPx;
    expect(targets).toEqual({ fine: 30, coarse: 44, minimum: 24 });
    const css = readFileSync(
      join(process.cwd(), "src/components/nle/nleWorkspace.css"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");
    const fine = css.match(
      /\.h3-nle-backdrop \{[^}]*--h3-nle-target:\s*(\d+)px/,
    );
    const coarse = css.match(
      /@media \(pointer: coarse\) \{\s*\.h3-nle-backdrop \{[^}]*--h3-nle-target:\s*(\d+)px/,
    );
    expect(Number(fine?.[1])).toBe(targets.fine);
    expect(Number(coarse?.[1])).toBe(targets.coarse);
    expect(targets.fine).toBeGreaterThanOrEqual(targets.minimum);
    expect(
      css.match(/\.h3-nle-dialog button \{[^}]*min-height:\s*([^;]+);/)?.[1],
    ).toBe("var(--h3-nle-target)");
  });
});
