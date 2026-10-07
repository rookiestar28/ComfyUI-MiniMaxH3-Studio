// M25-63 (B-M2563-13): one word for "timeline" per Chinese locale. The editor's timeline is titled
// 时间线 in zh-CN and 時間軸 in zh-TW; a card command or a Production label that says 时间轴 (or
// 時間線) on the same screen reads as a different thing. Every source file is scanned, not only
// the discovered copy tables, so a string outside a locale table cannot reintroduce the other
// word.

import { readdirSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { discoverLocaleTableFiles, readSource } from "./localeTableSource";

function sourceFiles(): readonly string[] {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..");
  const found: string[] = [];
  const walk = (dir: string): void => {
    for (const name of readdirSync(dir).sort()) {
      const abs = join(dir, name);
      if (statSync(abs).isDirectory()) walk(abs);
      else if (/\.tsx?$/.test(name))
        found.push(relative(root, abs).split(sep).join("/"));
    }
  };
  walk(join(root, "src"));
  return found;
}

const TERMS = [
  { locale: "zh-CN", used: "时间线", other: "时间轴" },
  { locale: "zh-TW", used: "時間軸", other: "時間線" },
] as const;

describe("B-M2563-13 timeline terminology", () => {
  it.each(TERMS)(
    "$locale says $used for the timeline everywhere in the frontend",
    ({ used, other }) => {
      const files = sourceFiles();
      expect(files).toContain("src/components/nle/nleCopy.ts");
      const offenders = files.filter((file) =>
        readSource(file).includes(other),
      );
      expect(offenders).toEqual([]);
      // The guard is live: the word the locale does use is present in the editor's copy.
      expect(readSource("src/components/nle/nleCopy.ts")).toContain(used);
    },
  );

  it("scans every discovered copy table", () => {
    const files = new Set(sourceFiles());
    for (const table of discoverLocaleTableFiles())
      expect(files.has(table), table).toBe(true);
  });
});
