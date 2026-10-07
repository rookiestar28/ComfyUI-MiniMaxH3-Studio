import { describe, expect, it, vi } from "vitest";

import { createLanguageSettingsAdapter } from "../src/host/languageSettings";
import {
  H3_LANGUAGE_SETTING_ID,
  createLocaleStore,
} from "../src/i18n/localeStore";

describe("M17-12 sole visible language settings adapter", () => {
  it("retains a valid pre-existing value without initialization write", () => {
    const write = vi.fn();
    const store = createLocaleStore();
    const adapter = createLanguageSettingsAdapter(store, {
      getSettingValue: () => "zh-TW",
      setSettingValueAsync: write,
    });
    expect(adapter.getSnapshot()).toEqual({
      status: "ready",
      value: "zh-TW",
      pending: false,
    });
    expect(store.getSnapshot().preference).toBe("zh-TW");
    expect(write).not.toHaveBeenCalled();
  });

  it("awaits host write and exact readback before committing", async () => {
    let value: unknown = "auto";
    const store = createLocaleStore();
    const write = vi.fn(async (id: string, next: unknown) => {
      expect(id).toBe(H3_LANGUAGE_SETTING_ID);
      value = next;
    });
    const adapter = createLanguageSettingsAdapter(store, {
      getSettingValue: () => value,
      setSettingValueAsync: write,
    });
    await expect(adapter.write("zh-CN")).resolves.toBe(true);
    expect(adapter.getSnapshot()).toEqual({
      status: "ready",
      value: "zh-CN",
      pending: false,
    });
    expect(store.getSnapshot().preference).toBe("zh-CN");
    await expect(adapter.write("invalid" as "en")).resolves.toBe(false);
    expect(write).toHaveBeenCalledTimes(1);
  });

  it("fails closed on missing method, mismatch, concurrent write and dispose", async () => {
    const store = createLocaleStore();
    const unavailable = createLanguageSettingsAdapter(store, {
      getSettingValue: () => "auto",
    });
    expect(unavailable.getSnapshot().status).toBe(
      "setting_storage_unavailable",
    );
    await expect(unavailable.write("en")).resolves.toBe(false);

    let resolveWrite!: () => void;
    let value: unknown = "auto";
    const adapter = createLanguageSettingsAdapter(store, {
      getSettingValue: () => value,
      setSettingValueAsync: () =>
        new Promise<void>((resolve) => {
          resolveWrite = resolve;
        }),
    });
    const pending = adapter.write("en");
    expect(adapter.getSnapshot().pending).toBe(true);
    await expect(adapter.write("zh-TW")).resolves.toBe(false);
    adapter.dispose();
    value = "zh-CN";
    resolveWrite();
    await expect(pending).resolves.toBe(false);
    expect(store.getSnapshot().preference).toBe("auto");
  });

  it("commits every allowed value including reset only after exact readback", async () => {
    let value: unknown = "auto";
    const store = createLocaleStore();
    const writes: unknown[] = [];
    const adapter = createLanguageSettingsAdapter(store, {
      getSettingValue: () => value,
      setSettingValueAsync: async (_id, next) => {
        writes.push(next);
        value = next;
      },
    });
    for (const next of ["en", "zh-TW", "zh-CN", "auto"] as const) {
      await expect(adapter.write(next)).resolves.toBe(true);
      expect(adapter.getSnapshot().value).toBe(next);
      expect(store.getSnapshot().preference).toBe(next);
    }
    expect(writes).toEqual(["en", "zh-TW", "zh-CN", "auto"]);
  });

  it("rolls back to a valid readback on mismatch or host exception", async () => {
    let value: unknown = "auto";
    const store = createLocaleStore();
    const mismatch = createLanguageSettingsAdapter(store, {
      getSettingValue: () => value,
      setSettingValueAsync: async () => {
        value = "zh-TW";
      },
    });
    await expect(mismatch.write("en")).resolves.toBe(false);
    expect(mismatch.getSnapshot()).toEqual({
      status: "write_failed",
      value: "zh-TW",
      pending: false,
    });
    expect(store.getSnapshot().preference).toBe("zh-TW");

    const failed = createLanguageSettingsAdapter(store, {
      getSettingValue: () => value,
      setSettingValueAsync: async () => {
        throw new Error("synthetic host rejection");
      },
    });
    await expect(failed.write("zh-CN")).resolves.toBe(false);
    expect(failed.getSnapshot()).toEqual({
      status: "write_failed",
      value: "zh-TW",
      pending: false,
    });
    expect(store.getSnapshot().preference).toBe("zh-TW");
  });
});
