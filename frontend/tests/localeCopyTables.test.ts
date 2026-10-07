import { describe, expect, it } from "vitest";

import {
  SUPPORTED_LOCALES,
  completeCatalog,
  validateCatalog,
} from "../src/i18n/catalog";
import {
  RUNTIME_VALIDATED_TABLE,
  UNEXPORTED_COPY_TABLES,
  discoverLocaleTableFiles,
  leafEntries,
  localeParityIssues,
  parseCopyTable,
  readCopyTable,
} from "./localeTableSource";

/**
 * M17-26 — locale parity for every copy table, not only the catalog.
 *
 * `validateCatalog` runs at module load and is fail-closed, but it can only see
 * `src/i18n/catalog.ts`. Four further locale-keyed tables live in component
 * source, hold 169 keys between them, and were guarded by author discipline
 * alone. This closes that gap.
 */
describe("M17-26 copy-table locale parity", () => {
  it("registers every locale table that exists in source", () => {
    const discovered = discoverLocaleTableFiles();
    const registered = [
      ...UNEXPORTED_COPY_TABLES.map((table) => table.file),
      RUNTIME_VALIDATED_TABLE,
    ].sort();
    // A new table added without registering it fails here, so the guard cannot
    // be defeated by forgetting the registry.
    expect(discovered).toEqual(registered);
  });

  it.each(UNEXPORTED_COPY_TABLES)(
    "keeps $file '$identifier' at full parity across the closed locale set",
    (table) => {
      const parsed = readCopyTable(table);
      const issues = localeParityIssues(parsed, SUPPORTED_LOCALES);
      expect(
        issues,
        issues
          .map((issue) => `${issue.locale} ${issue.kind}: ${issue.detail}`)
          .join("\n"),
      ).toEqual([]);
    },
  );

  it("reads a non-trivial number of keys from each table", () => {
    // Guards against a parser that silently returns an empty object and so
    // reports parity for a table it never actually read.
    for (const table of UNEXPORTED_COPY_TABLES) {
      const parsed = readCopyTable(table);
      const first = SUPPORTED_LOCALES[0];
      expect(
        leafEntries(parsed[first] as never).length,
        `${table.file} ${table.identifier}`,
      ).toBeGreaterThan(5);
    }
  });

  it("keeps the catalog itself under its own load-time contract", () => {
    expect(() => validateCatalog(completeCatalog)).not.toThrow();
    expect(Object.keys(completeCatalog).sort()).toEqual(
      [...SUPPORTED_LOCALES].sort(),
    );
  });
});

describe("M17-26 parity comparator", () => {
  const locales = ["en", "zh-TW", "zh-CN"] as const;
  const complete = {
    en: { a: "A", nested: { b: "B" } },
    "zh-TW": { a: "甲", nested: { b: "乙" } },
    "zh-CN": { a: "甲", nested: { b: "乙" } },
  };

  it("accepts a complete table", () => {
    expect(localeParityIssues(complete, locales)).toEqual([]);
  });

  it("rejects a key missing from one locale", () => {
    const broken = { ...complete, "zh-CN": { a: "甲", nested: {} } };
    expect(localeParityIssues(broken, locales)).toEqual([
      { locale: "zh-CN", kind: "missing-key", detail: "nested.b" },
    ]);
  });

  it("rejects a key present only in one locale", () => {
    const broken = {
      ...complete,
      "zh-TW": { a: "甲", extra: "多", nested: { b: "乙" } },
    };
    expect(localeParityIssues(broken, locales)).toEqual([
      { locale: "zh-TW", kind: "extra-key", detail: "extra" },
    ]);
  });

  it("rejects a whitespace-only value", () => {
    const broken = { ...complete, "zh-CN": { a: "  ", nested: { b: "乙" } } };
    expect(localeParityIssues(broken, locales)).toEqual([
      { locale: "zh-CN", kind: "blank-value", detail: "a" },
    ]);
  });

  it("rejects a missing locale and an unsupported extra locale", () => {
    const { "zh-CN": _dropped, ...missing } = complete;
    expect(localeParityIssues(missing, locales)).toEqual([
      {
        locale: "zh-CN",
        kind: "missing-locale",
        detail: "table has no 'zh-CN' block",
      },
    ]);
    const extra = { ...complete, ja: { a: "あ", nested: { b: "い" } } };
    expect(localeParityIssues(extra, locales)).toEqual([
      {
        locale: "ja",
        kind: "missing-locale",
        detail: "table declares unsupported locale 'ja'",
      },
    ]);
  });
});

describe("M17-26 copy-table reader", () => {
  it("reads literals, nesting, arrays and prettier string continuations", () => {
    const parsed = parseCopyTable(
      [
        "const copy = {",
        "  en: {",
        "    // a comment must not break the reader",
        "    plain: 'one',",
        "    wrapped:",
        '      "first half" +',
        '      " second half",',
        "    nested: { deep: `no interpolation here` },",
        '    list: ["zero", "one"],',
        "  },",
        "} as const;",
      ].join("\n"),
      "copy",
    );
    expect(leafEntries(parsed.en as never)).toEqual([
      ["plain", "one"],
      ["wrapped", "first half second half"],
      ["nested.deep", "no interpolation here"],
      ["list.0", "zero"],
      ["list.1", "one"],
    ]);
  });

  it("reads a table declared with a type annotation", () => {
    const parsed = parseCopyTable(
      'const LABELS: Readonly<Record<Locale, Labels>> = {\n  en: { a: "A" },\n};',
      "LABELS",
    );
    expect(leafEntries(parsed.en as never)).toEqual([["a", "A"]]);
  });

  it("fails closed on a value that is not literal data", () => {
    expect(() =>
      parseCopyTable("const copy = {\n  en: sharedCatalog.en,\n};", "copy"),
    ).toThrow(/unsupported copy-table value/);
    expect(() =>
      parseCopyTable(
        "const copy = {\n  en: { a: `value ${name}` },\n};",
        "copy",
      ),
    ).toThrow(/template interpolation/);
  });

  it("fails closed on a missing declaration", () => {
    expect(() => parseCopyTable("const other = {};", "copy")).toThrow(
      /no 'const copy' declaration/,
    );
  });
});
