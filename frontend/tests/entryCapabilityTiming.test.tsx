import { act, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * M23-36. The supplied host (ComfyUI 0.34.0, frontend 1.51.9) evaluates extension modules
 * before it creates its root graph: `app.graph` is `undefined` at that moment and exists by
 * the time `setup()` runs. App Mode capability must be decided when the sidebar is rendered
 * and when a run starts, never once at module evaluation -- otherwise a fully capable host is
 * reported as "seam unavailable" and every Task-stage control stays disabled.
 */

vi.mock("../src/styles/tokens.css?inline", () => ({
  default: ".h3c{}",
}));

type EntryHostFixture = typeof import("./fixtures/entryHostModules");

type EntryHostSettings = EventTarget & {
  getSettingValue(id: string): unknown;
  setSettingValue(id: string, value: unknown): void;
  setSettingValueAsync(id: string, value: unknown): Promise<void>;
};

function createEntryHostSettings(): EntryHostSettings {
  const values = new Map<string, unknown>([
    ["H3.Context.Language", "auto"],
    ["Comfy.Locale", "en"],
  ]);
  const settings = new EventTarget() as EntryHostSettings;
  settings.getSettingValue = (id) => values.get(id);
  settings.setSettingValue = (id, value) => values.set(id, value);
  settings.setSettingValueAsync = async (id, value) => {
    values.set(id, value);
  };
  return settings;
}

async function loadEntry(options: {
  graphInitializedBeforeSetup: boolean;
}): Promise<{ fixture: EntryHostFixture; container: HTMLElement }> {
  vi.resetModules();
  const fixture = await import("./fixtures/entryHostModules");
  (
    fixture.app as typeof fixture.app & {
      ui?: { settings: EntryHostSettings };
    }
  ).ui = { settings: createEntryHostSettings() };
  fixture.setGraph(fixture.canonicalGraph(17));
  // The host has not created its root graph while the extension module is evaluated.
  fixture.setGraphInitialized(false);
  await import("../src/entry");
  const extension = fixture.registeredExtension();
  fixture.setGraphInitialized(options.graphInitializedBeforeSetup);
  act(() => extension.setup?.());

  const panel = document.createElement("section");
  panel.className = "side-bar-panel";
  const content = document.createElement("div");
  content.className = "sidebar-content-container";
  const container = document.createElement("div");
  content.append(container);
  panel.append(content);
  document.body.append(panel);
  act(() => fixture.registeredTab().render(container));
  return { fixture, container };
}

function taskModeSelect(container: HTMLElement): HTMLSelectElement {
  const select = container.querySelector<HTMLSelectElement>(
    '[data-h3-focus-key="app-task-mode"]',
  );
  if (select === null) throw new Error("Task-stage task mode select is absent");
  return select;
}

describe("App Mode capability qualification timing", () => {
  afterEach(async () => {
    cleanup();
    document.body.innerHTML = "";
    const fixture = await import("./fixtures/entryHostModules");
    fixture.setGraphInitialized(true);
  });

  it("qualifies a host whose graph appears after module evaluation but before setup", async () => {
    const { container } = await loadEntry({
      graphInitializedBeforeSetup: true,
    });

    expect(container.querySelector(".h3-context-capability")).toBeNull();
    expect(taskModeSelect(container).disabled).toBe(false);
    expect(container.querySelector("[data-shell-status]")).not.toBeNull();
  });

  it("keeps the honest unavailable note while the host graph never appears", async () => {
    const { container } = await loadEntry({
      graphInitializedBeforeSetup: false,
    });

    expect(container.querySelector(".h3-context-capability")).not.toBeNull();
    expect(taskModeSelect(container).disabled).toBe(true);
  });
});
