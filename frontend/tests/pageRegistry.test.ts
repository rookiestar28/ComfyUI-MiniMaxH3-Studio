import { describe, expect, it, vi } from "vitest";

import { createPageRegistry } from "../src/navigation/pageRegistry";

describe("M17-12 top-level page registry", () => {
  it("owns Context, Production, and Settings in exact order", () => {
    const registry = createPageRegistry();
    expect(registry.getSnapshot()).toEqual({
      selected: "context",
      pages: [{ id: "context" }, { id: "production" }, { id: "settings" }],
    });
    const listener = vi.fn();
    registry.subscribe(listener);
    registry.register({ id: "production" });
    registry.register({ id: "production" });
    expect(registry.getSnapshot().pages).toEqual([
      { id: "context" },
      { id: "production" },
      { id: "settings" },
    ]);
    expect(listener).not.toHaveBeenCalled();
    expect(() => registry.register({ id: "unknown" as "context" })).toThrow(
      /unsupported page/i,
    );
  });

  it("preserves selection and rejects removal of every required page", () => {
    const registry = createPageRegistry();
    registry.select("production");
    const snapshot = registry.getSnapshot();
    expect(snapshot.selected).toBe("production");
    registry.register({ id: "production" });
    expect(registry.getSnapshot()).toBe(snapshot);
    expect(() => registry.unregister("production")).toThrow(/required page/i);
    expect(() => registry.unregister("context")).toThrow(/required page/i);
  });
});
