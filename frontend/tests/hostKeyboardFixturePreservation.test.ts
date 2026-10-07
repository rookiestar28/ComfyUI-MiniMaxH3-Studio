import { describe, expect, it, vi } from "vitest";
import { acquireModalKeyboardGuard } from "../src/host/modalKeyboardGuard";
import { probeCanvasKeyboardGuard } from "../src/host/canvasKeyboardGuard";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";

const variants = [
  { graph: false, load: false, workflow: false },
  { graph: true, load: false, workflow: false },
  { graph: true, load: true, workflow: false },
  { graph: false, load: false, workflow: true },
  { graph: true, load: false, workflow: true },
  { graph: true, load: true, workflow: true },
] as const;

function base(variant: (typeof variants)[number]) {
  return {
    ...(variant.graph
      ? { graph: { serialize: () => ({ nodes: [], links: [] }) } }
      : {}),
    ...(variant.load ? { loadGraphData: async () => undefined } : {}),
  };
}

describe("fixture app capability preservation", () => {
  it.each(variants)(
    "preserves foreign capability accessors without invoking them: %j",
    (variant) => {
      const host = base(variant);
      const constructorGetter = vi.fn(() => {
        throw new Error("foreign constructor getter");
      });
      const canvasGetter = vi.fn(() => {
        throw new Error("foreign canvas getter");
      });
      Object.defineProperty(host, "constructor", {
        get: constructorGetter,
        enumerable: true,
        configurable: true,
      });
      Object.defineProperty(host, "canvas", {
        get: canvasGetter,
        enumerable: true,
        configurable: true,
      });
      const before = Object.getOwnPropertyDescriptors(host);
      const bound = fixtureBackedAppModeHost(
        host,
        {},
        variant.workflow ? { workflowVariant: "existing_active" } : {},
      ).app;
      expect(Object.getPrototypeOf(bound)).toBe(Object.getPrototypeOf(host));
      for (const name of ["constructor", "canvas"])
        expect(Object.getOwnPropertyDescriptor(bound, name)).toEqual(
          before[name],
        );
      expect(acquireModalKeyboardGuard(bound).status).toBe("unavailable");
      expect(probeCanvasKeyboardGuard(bound).status).toBe("unavailable");
      expect(constructorGetter).not.toHaveBeenCalled();
      expect(canvasGetter).not.toHaveBeenCalled();
    },
  );

  it.each(variants)(
    "preserves explicit incompatible constructor/canvas data through every clone: %j",
    (variant) => {
      const host = base(variant);
      Object.defineProperty(host, "constructor", {
        value: false,
        enumerable: false,
        configurable: true,
        writable: false,
      });
      Object.defineProperty(host, "canvas", {
        value: null,
        enumerable: false,
        configurable: true,
        writable: false,
      });
      const bound = fixtureBackedAppModeHost(
        host,
        {},
        variant.workflow ? { workflowVariant: "existing_active" } : {},
      ).app;
      expect(Object.getOwnPropertyDescriptor(bound, "constructor")).toEqual(
        Object.getOwnPropertyDescriptor(host, "constructor"),
      );
      expect(Object.getOwnPropertyDescriptor(bound, "canvas")).toEqual(
        Object.getOwnPropertyDescriptor(host, "canvas"),
      );
      expect(acquireModalKeyboardGuard(bound).status).toBe("unavailable");
      expect(probeCanvasKeyboardGuard(bound).status).toBe("unavailable");
    },
  );

  it("supplies canonical capabilities only to an ordinary plain app with no explicit shape", () => {
    const bound = fixtureBackedAppModeHost({}, {}).app;
    const result = acquireModalKeyboardGuard(bound);
    expect(result.status).toBe("ready");
    if (result.status === "ready") result.value.release();
    expect(probeCanvasKeyboardGuard(bound).status).toBe("ready");
    const foreign = Object.create({
      constructor: null,
      canvas: null,
    }) as object;
    const kept = fixtureBackedAppModeHost(foreign, {}).app;
    expect(Object.getPrototypeOf(kept)).toBe(Object.getPrototypeOf(foreign));
    expect(acquireModalKeyboardGuard(kept).status).toBe("unavailable");
    expect(probeCanvasKeyboardGuard(kept).status).toBe("unavailable");
  });
});
