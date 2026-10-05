import {
  H3_LANGUAGE_SETTING_ID,
  type LocalePreference,
  type LocaleStore,
} from "../i18n/localeStore";

export type LanguageSettingStatus =
  | "ready"
  | "setting_value_invalid"
  | "setting_storage_unavailable"
  | "write_failed";

export type LanguageSettingSnapshot = Readonly<{
  status: LanguageSettingStatus;
  value: LocalePreference | undefined;
  pending: boolean;
}>;

export type LanguageSettingsSeam = {
  getSettingValue?(id: string): unknown;
  setSettingValueAsync?(id: string, value: unknown): Promise<unknown>;
};

export type LanguageSettingsAdapter = {
  getSnapshot(): LanguageSettingSnapshot;
  subscribe(listener: () => void): () => void;
  write(value: LocalePreference): Promise<boolean>;
  dispose(): void;
};

const values = new Set<LocalePreference>(["auto", "en", "zh-TW", "zh-CN"]);

export function isLocalePreference(value: unknown): value is LocalePreference {
  return typeof value === "string" && values.has(value as LocalePreference);
}

export function createLanguageSettingsAdapter(
  store: LocaleStore,
  settings: LanguageSettingsSeam,
): LanguageSettingsAdapter {
  const listeners = new Set<() => void>();
  const read = (): LocalePreference | undefined => {
    if (typeof settings.getSettingValue !== "function") return undefined;
    try {
      const value = settings.getSettingValue(H3_LANGUAGE_SETTING_ID);
      return isLocalePreference(value) ? value : undefined;
    } catch {
      return undefined;
    }
  };
  const initial = read();
  const available =
    typeof settings.getSettingValue === "function" &&
    typeof settings.setSettingValueAsync === "function";
  let snapshot: LanguageSettingSnapshot = Object.freeze({
    status: !available
      ? "setting_storage_unavailable"
      : initial === undefined
        ? "setting_value_invalid"
        : "ready",
    value: initial,
    pending: false,
  });
  if (initial !== undefined) store.setPreference(initial);
  let generation = 0;
  let disposed = false;
  const publish = (next: LanguageSettingSnapshot): void => {
    snapshot = Object.freeze(next);
    for (const listener of [...listeners]) listener();
  };
  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      if (disposed) return () => undefined;
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    async write(value) {
      if (
        disposed ||
        snapshot.pending ||
        !isLocalePreference(value) ||
        typeof settings.setSettingValueAsync !== "function" ||
        typeof settings.getSettingValue !== "function"
      )
        return false;
      const operation = ++generation;
      const prior = snapshot.value;
      publish({ status: snapshot.status, value: prior, pending: true });
      try {
        await settings.setSettingValueAsync(H3_LANGUAGE_SETTING_ID, value);
        const readback = read();
        if (disposed || generation !== operation) return false;
        if (readback !== value) {
          if (readback !== undefined) store.setPreference(readback);
          publish({
            status: "write_failed",
            value: readback ?? prior,
            pending: false,
          });
          return false;
        }
        store.setPreference(readback);
        publish({ status: "ready", value: readback, pending: false });
        return true;
      } catch {
        if (disposed || generation !== operation) return false;
        const readback = read();
        if (readback !== undefined) store.setPreference(readback);
        publish({
          status: "write_failed",
          value: readback ?? prior,
          pending: false,
        });
        return false;
      }
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      generation += 1;
      listeners.clear();
    },
  };
}
