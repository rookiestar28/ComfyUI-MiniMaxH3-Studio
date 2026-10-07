import { describe, expect, it } from "vitest";

import {
  CATALOG_KEYS,
  SUPPORTED_LOCALES,
  type CatalogKey,
  completeCatalog,
  sidebarCopy,
  stageCopy,
  pageCopy,
  translate,
  validateCatalog,
} from "../src/i18n/catalog";

describe("M17-00 typed locale catalog", () => {
  it("keeps one closed complete catalog for en, zh-TW, and zh-CN", () => {
    expect(SUPPORTED_LOCALES).toEqual(["en", "zh-TW", "zh-CN"]);
    expect(new Set(CATALOG_KEYS).size).toBe(CATALOG_KEYS.length);
    for (const locale of SUPPORTED_LOCALES) {
      const values = CATALOG_KEYS.map((key) => translate(locale, key));
      expect(values.every((value) => value.trim().length > 0)).toBe(true);
      expect(values.some((value) => value.includes("[Missing]"))).toBe(false);
    }
    expect(sidebarCopy("zh-CN").startAppMode).toBe("启动 H3 App Mode");
    expect(stageCopy("zh-CN").labels.execute).toBe("执行／导出");
    expect(pageCopy("zh-TW").production).toBe("導演台");
    expect(pageCopy("zh-CN").production).toBe("导演台");
    expect(sidebarCopy("en").sourceBlockers.distinctFrames).toBe(
      "Select distinct first and last frame sources before submitting.",
    );
    expect(sidebarCopy("zh-TW").sourceBlockers.reference).toBe(
      "提交前請至少選擇一個參考來源。",
    );
    expect(sidebarCopy("zh-CN").sourceBlockers.firstFrame).toBe(
      "提交前请选择首帧来源。",
    );
  });

  it("falls back to English without interpreting catalog values as HTML", () => {
    expect(translate("unsupported", "startAppMode")).toBe("Start H3 App Mode");
    for (const locale of SUPPORTED_LOCALES)
      for (const key of CATALOG_KEYS)
        expect(translate(locale, key)).not.toMatch(/<\/?[a-z][^>]*>/i);
  });

  it("fails closed for a runtime key outside the typed catalog", () => {
    expect(() =>
      translate("en", "sidebar.not-a-declared-key" as CatalogKey),
    ).toThrow(/catalog key/i);
  });

  it("rejects missing, extra, and malformed locale entries", () => {
    const missing = structuredClone(completeCatalog) as Record<string, any>;
    delete missing["zh-CN"].sidebar.startAppMode;
    expect(() => validateCatalog(missing)).toThrow(/key parity/i);

    const extra = structuredClone(completeCatalog) as Record<string, any>;
    extra["zh-TW"].sidebar.undeclared = "不可接受";
    expect(() => validateCatalog(extra)).toThrow(/key parity/i);

    const malformed = structuredClone(completeCatalog) as Record<string, any>;
    malformed["zh-CN"].sidebar.startAppMode = "  ";
    expect(() => validateCatalog(malformed)).toThrow(/malformed/i);
  });
});
