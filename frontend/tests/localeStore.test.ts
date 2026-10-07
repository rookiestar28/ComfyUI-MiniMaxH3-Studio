import { describe, expect, it, vi } from "vitest";

import {
  H3_LANGUAGE_SETTING_ID,
  bindLocaleStoreToHost,
  createLanguageSetting,
  createLocaleStore,
  normalizeLocale,
} from "../src/i18n/localeStore";

describe("M17-00 presentation-only locale store", () => {
  it("normalizes only the closed aliases and falls back to English", () => {
    expect(normalizeLocale("zh_TW")).toBe("zh-TW");
    expect(normalizeLocale("zh-Hant-HK")).toBe("zh-TW");
    expect(normalizeLocale("zh_CN")).toBe("zh-CN");
    expect(normalizeLocale("zh-Hans-SG")).toBe("zh-CN");
    expect(normalizeLocale("en-US")).toBe("en");
    expect(normalizeLocale("fr-FR")).toBe("en");
    expect(normalizeLocale({ locale: "zh-TW" })).toBe("en");
  });

  it("switches live without replacing the store or notifying unchanged state", () => {
    const store = createLocaleStore("auto", "en-US");
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    store.setHostLocale("zh_TW");
    expect(store.getSnapshot()).toEqual({
      preference: "auto",
      locale: "zh-TW",
    });
    store.setPreference("zh-CN");
    store.setHostLocale("en-US");
    expect(store.getSnapshot()).toEqual({
      preference: "zh-CN",
      locale: "zh-CN",
    });
    store.setPreference("invalid");
    expect(store.getSnapshot()).toEqual({
      preference: "auto",
      locale: "en",
    });
    const calls = listener.mock.calls.length;
    store.setHostLocale("en");
    expect(listener).toHaveBeenCalledTimes(calls);
    unsubscribe();
  });

  it("uses the supported extension setting and owned Comfy.Locale event seam", () => {
    const values = new Map<string, unknown>([
      [H3_LANGUAGE_SETTING_ID, "auto"],
      ["Comfy.Locale", "zh_TW"],
    ]);
    const target = new EventTarget();
    const settings = {
      getSettingValue: (id: string) => values.get(id),
      addEventListener: target.addEventListener.bind(target),
      removeEventListener: target.removeEventListener.bind(target),
    };
    const store = createLocaleStore();
    const descriptor = createLanguageSetting(store, settings);
    expect(descriptor).toMatchObject({
      id: H3_LANGUAGE_SETTING_ID,
      type: "hidden",
      defaultValue: "auto",
    });
    expect(descriptor).not.toHaveProperty("options");
    const dispose = bindLocaleStoreToHost(store, settings);
    expect(store.getSnapshot().locale).toBe("zh-TW");
    values.set("Comfy.Locale", "zh_CN");
    target.dispatchEvent(
      new CustomEvent("Comfy.Locale.change", {
        detail: { value: "zh_CN", oldValue: "zh_TW" },
      }),
    );
    expect(store.getSnapshot().locale).toBe("zh-CN");
    descriptor.onChange("en");
    expect(store.getSnapshot()).toEqual({ preference: "en", locale: "en" });
    dispose();
    values.set("Comfy.Locale", "zh_TW");
    target.dispatchEvent(
      new CustomEvent("Comfy.Locale.change", { detail: { value: "zh_TW" } }),
    );
    expect(store.getSnapshot().locale).toBe("en");
  });
});
