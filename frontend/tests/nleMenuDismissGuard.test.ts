// M25-79 (B-M2579-01): the class guard for floating editor menus. The timeline's clip and track
// menus shipped without an outside-press dismissal because it lived privately in the bin's menus,
// and no test asked every floating menu the same question. This one does, from the source: each
// `h3-nle-context-menu` an editor component renders registers `useMenuDismiss`, and any other
// `role="menu"` is a pinned exception with its reason. A new floating menu fails here until it
// dismisses or is pinned.

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const NLE = resolve(HERE, "..", "src", "components", "nle");

/**
 * `role="menu"` elements that are not floating context menus. The toolbar's "More timeline tools"
 * overflow is a tool tray its own button opens and closes; journeys use its scroll range and
 * alternatives across presses elsewhere, so it does not close on an outside press (M25-79 O-1).
 */
const PINNED_OTHER_MENUS: Readonly<Record<string, number>> = {
  "NleTimelineToolbar.tsx": 1,
};

function sources(directory: string): [string, string][] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sources(path);
    return entry.name.endsWith(".tsx")
      ? [
          [
            relative(NLE, path).replaceAll("\\", "/"),
            readFileSync(path, "utf8"),
          ],
        ]
      : [];
  });
}

const count = (text: string, pattern: RegExp) =>
  (text.match(pattern) ?? []).length;
const CONTEXT_MENU = /className="h3-nle-context-menu[" ]/g;
const ROLE_MENU = /role="menu"/g;
const DISMISS_CALL = /(?<!function )\buseMenuDismiss\(/g;

describe("M25-79 floating editor menus close on an outside press", () => {
  const files = sources(NLE);

  it("finds the editor's menus", () => {
    const withMenus = files.filter(([, text]) => count(text, ROLE_MENU) > 0);
    expect(withMenus.map(([file]) => file).sort()).toEqual([
      "NleBinMenus.tsx",
      "NleTimelineMenus.tsx",
      "NleTimelineToolbar.tsx",
    ]);
  });

  it("registers useMenuDismiss once for each floating context menu", () => {
    for (const [file, text] of files)
      expect({ file, dismissals: count(text, DISMISS_CALL) }).toEqual({
        file,
        dismissals: count(text, CONTEXT_MENU),
      });
  });

  it("pins every other role=menu with its reason", () => {
    for (const [file, text] of files)
      expect({
        file,
        others: count(text, ROLE_MENU) - count(text, CONTEXT_MENU),
      }).toEqual({ file, others: PINNED_OTHER_MENUS[file] ?? 0 });
  });
});
