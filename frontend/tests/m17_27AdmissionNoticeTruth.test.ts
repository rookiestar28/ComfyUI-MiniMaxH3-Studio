import { describe, expect, it } from "vitest";

import { SUPPORTED_LOCALES, sidebarCopy } from "../src/i18n/catalog";

/**
 * M17-27 — each admission verdict must say what it measured and which route it
 * judges.
 *
 * The panel renders three verdicts about three different routes at once. The
 * missing-asset notice is computed for the replace route, because
 * `admissionUseExisting` is false while a decision is pending and only
 * `admissionBlocksQueue` can be true on `missing_asset`. The incompatible
 * headline is a verdict on binding the visible graph. Neither used to say so,
 * and the missing-asset copy additionally asserted something the adapter never
 * measured: it claimed the host had no installed model for a role, when what
 * `observe_slots` measures is whether one specific published default filename
 * resolves verbatim. On a host with five H3 diffusion weights installed that
 * sentence is false while the measurement under it is true.
 *
 * These rows are deliberately about the copy rather than about a render. The
 * defect was never that the component chose the wrong string; it was that the
 * string said the wrong thing, in every locale.
 */

/** Phrasings that assert a role-level absence the adapter never measured. */
const ROLE_ABSENCE_CLAIMS = [
  "no installed model",
  "沒有安裝",
  "没有安装",
] as const;

/** Each locale's own way of naming the thing that would be created. */
const CREATED_WORKFLOW = {
  en: "would create",
  "zh-TW": "要建立的流程",
  "zh-CN": "要创建的流程",
} as const;

/**
 * Each locale's own way of saying the connect route is unaffected.
 *
 * M21-05 removed this sentence. It is kept here as the thing that must now be
 * *absent*: the notice no longer has to disclaim itself, because it renders
 * inside the affordance it judges instead of above the one it does not.
 */
const CONNECT_UNAFFECTED = {
  en: "Connecting to your current canvas is not affected",
  "zh-TW": "連接目前畫布不受此限制",
  "zh-CN": "连接当前画布不受此限制",
} as const;

/** Each locale's own way of naming the bind route in the incompatible verdict. */
const BIND_ROUTE = {
  en: "cannot be bound",
  "zh-TW": "無法直接綁定",
  "zh-CN": "无法直接绑定",
} as const;

describe("M17-27 the missing-asset notice states what was measured", () => {
  it.each(SUPPORTED_LOCALES)(
    "does not claim the host has no model for the role (%s)",
    (locale) => {
      const copy = sidebarCopy(locale).generation.missingAsset;
      for (const claim of ROLE_ABSENCE_CLAIMS) {
        expect(copy).not.toContain(claim);
      }
    },
  );

  it.each(SUPPORTED_LOCALES)(
    "names the workflow the verdict is about (%s)",
    (locale) => {
      const copy = sidebarCopy(locale).generation.missingAsset;
      expect(copy).toContain(CREATED_WORKFLOW[locale]);
      // The role list is what the notice exists to carry; losing the
      // placeholder would make it a generic warning again.
      expect(copy).toContain("{roles}");
    },
  );

  it.each(SUPPORTED_LOCALES)(
    "no longer has to disclaim the connect route (%s)",
    (locale) => {
      // M21-05. The sentence was true and necessary while the notice rendered
      // above the connect section. Scoping the notice to the materialize
      // actions is what makes it unnecessary, so its continued presence would
      // mean the placement regressed.
      const copy = sidebarCopy(locale).generation.missingAsset;
      expect(copy).not.toContain(CONNECT_UNAFFECTED[locale]);
    },
  );
});

describe("M17-27 the incompatible verdict names the route it judges", () => {
  it.each(SUPPORTED_LOCALES)(
    "names binding the visible graph (%s)",
    (locale) => {
      expect(sidebarCopy(locale).incompatible).toContain(BIND_ROUTE[locale]);
    },
  );

  it.each(SUPPORTED_LOCALES)(
    "still offers the connect route as a way forward (%s)",
    (locale) => {
      // A verdict that only offers keep-or-replace reads as a blanket rejection
      // of the canvas, which is what sent a user looking for a defect that was
      // not there. Connect is available on any graph carrying a native anchor.
      expect(sidebarCopy(locale).incompatible).toMatch(/connect|連接|连接/i);
    },
  );
});

/**
 * M17-28 — the relocated verdict must not inherit the missing one's claim.
 *
 * `missing_asset` used to fire for a host that had every official weight
 * installed, just not under the exact filename the template materializes with.
 * Admission now accepts those names, and the case that remains is a different
 * statement: the weights are here, the workflow names other ones. A copy that
 * still said "this host does not have" would reintroduce the defect in words
 * after it was fixed in the matcher.
 */

/** Each locale's own way of saying the host lacks the weight. */
const HOST_LACKS = {
  en: "could not all be verified",
  "zh-TW": "尚未全部核對",
  "zh-CN": "尚未全部核对",
} as const;

/** Each locale's own way of saying the host reports every required weight. */
const HOST_REPORTS_ALL = {
  en: "installed official",
  "zh-TW": "已安裝",
  "zh-CN": "已安装",
} as const;

describe("M17-28 the relocated notice says the weights are installed", () => {
  it.each(SUPPORTED_LOCALES)(
    "does not claim the host lacks the weight (%s)",
    (locale) => {
      const copy = sidebarCopy(locale).generation.assetRelocated;
      expect(copy).not.toContain(HOST_LACKS[locale]);
      for (const claim of ROLE_ABSENCE_CLAIMS)
        expect(copy).not.toContain(claim);
    },
  );

  it.each(SUPPORTED_LOCALES)(
    "describes conservative installed official selection (%s)",
    (locale) => {
      expect(sidebarCopy(locale).generation.assetRelocated).toContain(
        HOST_REPORTS_ALL[locale],
      );
    },
  );

  it.each(SUPPORTED_LOCALES)(
    "keeps the invariants the missing-asset notice carries (%s)",
    (locale) => {
      const copy = sidebarCopy(locale).generation.assetRelocated;
      expect(copy).toContain(CREATED_WORKFLOW[locale]);
      expect(copy).toContain("{roles}");
      expect(copy).not.toContain(CONNECT_UNAFFECTED[locale]);
    },
  );

  it.each(SUPPORTED_LOCALES)(
    "is a different sentence from the missing-asset one (%s)",
    (locale) => {
      const generation = sidebarCopy(locale).generation;
      expect(generation.assetRelocated).not.toBe(generation.missingAsset);
    },
  );

  it.each(SUPPORTED_LOCALES)(
    "states unresolved names without claiming all models are absent (%s)",
    (locale) => {
      // Unrecognized names are not proof that every usable model is absent.
      expect(sidebarCopy(locale).generation.missingAsset).toContain(
        HOST_LACKS[locale],
      );
    },
  );
});
