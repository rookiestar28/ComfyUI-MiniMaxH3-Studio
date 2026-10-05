import type { Locale } from "./catalog";

export const H3_LANGUAGE_SETTING_ID = "H3.Context.Language";
export type LocalePreference = "auto" | Locale;
export type LocaleSnapshot = Readonly<{
  preference: LocalePreference;
  locale: Locale;
}>;

export type PublicSettingsSeam = {
  getSettingValue(id: string): unknown;
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
};

export type LocaleStore = {
  getSnapshot(): LocaleSnapshot;
  subscribe(listener: () => void): () => void;
  setPreference(value: unknown): void;
  setHostLocale(value: unknown): void;
};

export function normalizeLocale(value: unknown): Locale {
  if (typeof value !== "string") return "en";
  const locale = value.trim().replaceAll("_", "-").toLowerCase();
  if (
    locale === "zh-tw" ||
    locale.startsWith("zh-hant") ||
    locale === "zh-hk" ||
    locale === "zh-mo"
  )
    return "zh-TW";
  if (locale === "zh-cn" || locale.startsWith("zh-hans") || locale === "zh-sg")
    return "zh-CN";
  return locale === "en" || locale.startsWith("en-") ? "en" : "en";
}

function normalizePreference(value: unknown): LocalePreference {
  if (
    value === "auto" ||
    value === "en" ||
    value === "zh-TW" ||
    value === "zh-CN"
  )
    return value;
  return "auto";
}

export function createLocaleStore(
  initialPreference: unknown = "auto",
  initialHostLocale: unknown = "en",
): LocaleStore {
  let preference = normalizePreference(initialPreference);
  let hostLocale = normalizeLocale(initialHostLocale);
  let snapshot: LocaleSnapshot = Object.freeze({
    preference,
    locale: preference === "auto" ? hostLocale : preference,
  });
  const listeners = new Set<() => void>();
  const publish = (): void => {
    const locale = preference === "auto" ? hostLocale : preference;
    if (snapshot.preference === preference && snapshot.locale === locale)
      return;
    snapshot = Object.freeze({ preference, locale });
    for (const listener of [...listeners]) listener();
  };
  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setPreference(value) {
      preference = normalizePreference(value);
      publish();
    },
    setHostLocale(value) {
      hostLocale = normalizeLocale(value);
      publish();
    },
  };
}

export function createLanguageSetting(
  store: LocaleStore,
  _settings?: Pick<PublicSettingsSeam, "getSettingValue">,
) {
  return {
    id: H3_LANGUAGE_SETTING_ID,
    name: "H3 Context language",
    type: "hidden" as const,
    defaultValue: "auto",
    onChange(value: unknown): void {
      store.setPreference(value);
    },
  };
}

export function bindLocaleStoreToHost(
  store: LocaleStore,
  settings: PublicSettingsSeam,
): () => void {
  store.setPreference(settings.getSettingValue(H3_LANGUAGE_SETTING_ID));
  store.setHostLocale(settings.getSettingValue("Comfy.Locale"));
  const onHostLocale: EventListener = (event) => {
    const detail = (event as CustomEvent<unknown>).detail;
    const value =
      detail !== null && typeof detail === "object" && "value" in detail
        ? (detail as { value?: unknown }).value
        : detail;
    store.setHostLocale(value);
  };
  settings.addEventListener("Comfy.Locale.change", onHostLocale);
  return () =>
    settings.removeEventListener("Comfy.Locale.change", onHostLocale);
}
