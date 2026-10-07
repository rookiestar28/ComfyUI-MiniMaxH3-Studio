import { describe, expect, it, vi } from "vitest";

import {
  availableSidebarWidth,
  createSidebarWidthController,
  MIN_SIDEBAR_WIDTH_PX,
} from "../src/host/sidebarWidth";

function setWidth(element: HTMLElement, width: number): void {
  setHorizontalBounds(element, 0, width);
}

function setHorizontalBounds(
  element: HTMLElement,
  left: number,
  right: number,
): void {
  Object.defineProperty(element, "getBoundingClientRect", {
    configurable: true,
    value: () => ({
      bottom: 0,
      height: 0,
      left,
      right,
      toJSON: () => undefined,
      top: 0,
      width: right - left,
      x: left,
      y: 0,
    }),
  });
}

function setStyleAwareWidth(element: HTMLElement, naturalWidth: number): void {
  Object.defineProperty(element, "getBoundingClientRect", {
    configurable: true,
    value: () => {
      const inlineMinimum = Number.parseFloat(element.style.minWidth) || 0;
      const inlineWidth = Number.parseFloat(element.style.width) || 0;
      const width = Math.max(naturalWidth, inlineMinimum, inlineWidth);
      return {
        bottom: 0,
        height: 0,
        left: 0,
        right: width,
        toJSON: () => undefined,
        top: 0,
        width,
        x: 0,
        y: 0,
      };
    },
  });
}

describe("H3 sidebar width ownership", () => {
  it("observes viewport-only resize and removes the owned listener on disposal", () => {
    const host = document.createElement("div");
    host.className = "side-bar-panel";
    host.style.position = "fixed";
    host.style.left = "58px";
    host.style.right = "auto";
    host.style.width = "233px";
    host.style.minWidth = "11px";
    host.style.flexBasis = "17px";
    setHorizontalBounds(host, 58, 291);
    const mount = document.createElement("div");
    host.append(mount);
    document.body.append(host);
    const previousInnerWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1280,
    });
    const scheduled: Array<() => void> = [];
    const cancel = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = createSidebarWidthController(mount, {
        schedule: (callback) => {
          scheduled.push(callback);
          return scheduled.length - 1;
        },
        cancel,
      });
      expect(host.style.width).toBe("704px");
      scheduled[0]?.();
      setHorizontalBounds(host, 58, 762);
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: 360,
      });
      window.dispatchEvent(new Event("resize"));
      window.dispatchEvent(new Event("resize"));
      expect(scheduled).toHaveLength(2);
      scheduled[1]?.();
      expect(host.style.minWidth).toBe("302px");
      expect(host.style.width).toBe("302px");
      setHorizontalBounds(host, 58, 360);
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: 1280,
      });
      window.dispatchEvent(new Event("resize"));
      expect(scheduled).toHaveLength(3);
      scheduled[2]?.();
      expect(host.style.minWidth).toBe("704px");
      expect(host.style.width).toBe("704px");
      window.dispatchEvent(new Event("resize"));
      expect(scheduled).toHaveLength(4);
      dispose();
      dispose();
      expect(cancel).toHaveBeenCalledExactlyOnceWith(3);
      scheduled[3]?.();
      window.dispatchEvent(new Event("resize"));
      expect(scheduled).toHaveLength(4);
      expect(host.style.minWidth).toBe("11px");
      expect(host.style.width).toBe("233px");
      expect(host.style.flexBasis).toBe("17px");
      expect(mount.style.minWidth).toBe("");
    } finally {
      dispose?.();
      host.remove();
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: previousInnerWidth,
      });
    }
  });

  it.each([
    {
      surfaceLeft: 58,
      surfaceRight: 360,
      hostLeft: 58,
      hostRight: 418,
      left: "58px",
      right: "auto",
    },
    {
      surfaceLeft: 0,
      surfaceRight: 302,
      hostLeft: -58,
      hostRight: 302,
      left: "auto",
      right: "58px",
    },
  ])(
    "respects the fixed owner's inline anchor when the canvas is absent: $left/$right",
    ({ surfaceLeft, surfaceRight, hostLeft, hostRight, left, right }) => {
      const splitter = document.createElement("div");
      setHorizontalBounds(splitter, surfaceLeft, surfaceRight);
      const host = document.createElement("div");
      host.className = "side-bar-panel";
      host.style.minWidth = "11px";
      host.style.width = "360px";
      host.style.flexBasis = "360px";
      host.style.position = "fixed";
      host.style.left = left;
      host.style.right = right;
      setHorizontalBounds(host, hostLeft, hostRight);
      const content = document.createElement("div");
      content.className = "sidebar-content-container";
      content.style.minWidth = "13px";
      const mount = document.createElement("div");
      mount.style.minWidth = "19px";
      const unrelated = document.createElement("div");
      unrelated.className = "side-bar-panel";
      unrelated.style.minWidth = "9px";
      content.append(mount);
      host.append(content);
      splitter.append(host);
      document.body.append(splitter, unrelated);
      const previousInnerWidth = window.innerWidth;
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: 360,
      });
      const scheduled: Array<() => void> = [];
      let dispose: (() => void) | undefined;
      try {
        dispose = createSidebarWidthController(mount, {
          schedule: (callback) => {
            scheduled.push(callback);
            return scheduled.length - 1;
          },
          cancel: () => undefined,
        });
        expect(host.style.minWidth).toBe("302px");
        expect(host.style.width).toBe("302px");
        expect(host.style.flexBasis).toBe("302px");
        expect(content.style.minWidth).toBe("302px");
        expect(mount.style.minWidth).toBe("302px");
        expect(unrelated.style.minWidth).toBe("9px");
        setHorizontalBounds(host, surfaceLeft, surfaceRight);
        scheduled[0]?.();
        expect(host.style.minWidth).toBe("302px");
        expect(host.style.width).toBe("302px");
        Object.defineProperty(window, "innerWidth", {
          configurable: true,
          value: 1280,
        });
        setHorizontalBounds(
          host,
          left === "auto" ? 920 : 58,
          left === "auto" ? 1222 : 360,
        );
        scheduled[0]?.();
        expect(host.style.minWidth).toBe("704px");
        expect(host.style.width).toBe("704px");
        Object.defineProperty(window, "innerWidth", {
          configurable: true,
          value: 360,
        });
        setHorizontalBounds(host, hostLeft, hostRight);
        scheduled[0]?.();
        expect(host.style.minWidth).toBe("302px");
        expect(host.style.width).toBe("302px");
        dispose();
        scheduled[0]?.();
        expect(host.style.minWidth).toBe("11px");
        expect(host.style.width).toBe("360px");
        expect(host.style.flexBasis).toBe("360px");
        expect(content.style.minWidth).toBe("13px");
        expect(mount.style.minWidth).toBe("19px");
        expect(unrelated.style.minWidth).toBe("9px");
      } finally {
        dispose?.();
        splitter.remove();
        unrelated.remove();
        Object.defineProperty(window, "innerWidth", {
          configurable: true,
          value: previousInnerWidth,
        });
      }
    },
  );

  it.each([
    { position: "relative", left: "58px", right: "auto" },
    { position: "fixed", left: "auto", right: "auto" },
    { position: "fixed", left: "58px", right: "58px" },
    { position: "fixed", left: "", right: "58px" },
  ])(
    "preserves the fallback for an unqualified anchor: $position/$left/$right",
    ({ position, left, right }) => {
      const host = document.createElement("div");
      host.className = "side-bar-panel";
      host.style.position = position;
      host.style.left = left;
      host.style.right = right;
      setHorizontalBounds(host, 58, 418);
      const mount = document.createElement("div");
      host.append(mount);
      document.body.append(host);
      const previousInnerWidth = window.innerWidth;
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: 360,
      });
      let dispose: (() => void) | undefined;
      try {
        dispose = createSidebarWidthController(mount, {
          schedule: () => 0,
          cancel: () => undefined,
        });
        expect(host.style.minWidth).toBe("360px");
        expect(host.style.left).toBe(left);
        expect(host.style.right).toBe(right);
      } finally {
        dispose?.();
        host.remove();
        Object.defineProperty(window, "innerWidth", {
          configurable: true,
          value: previousInnerWidth,
        });
      }
    },
  );

  it("TOPOLOGY-01: clamps left/right owners to canvas and opposite-panel space", () => {
    expect(
      availableSidebarWidth({
        viewportWidth: 1280,
        host: { left: 0, right: 480 },
        opposite: { left: 800, right: 1280 },
        canvas: { left: 0, right: 1280 },
      }),
    ).toBe(800);
    expect(
      availableSidebarWidth({
        viewportWidth: 1280,
        host: { left: 800, right: 1280 },
        opposite: { left: 0, right: 480 },
        canvas: { left: 0, right: 1280 },
      }),
    ).toBe(800);
    expect(
      availableSidebarWidth({
        viewportWidth: 480,
        host: { left: 0, right: 480 },
        opposite: { left: 300, right: 480 },
        canvas: { left: 0, right: 480 },
      }),
    ).toBe(300);
  });

  it("keeps an adjacent PrimeVue canvas in the same stable available surface", () => {
    const splitter = document.createElement("div");
    splitter.className = "p-splitter";
    splitter.dataset.pcName = "splitter";
    const host = document.createElement("div");
    host.className = "p-splitterpanel side-bar-panel";
    host.style.width = "233px";
    host.style.flexBasis = "17px";
    setHorizontalBounds(host, 0, 233);
    const content = document.createElement("div");
    content.className = "sidebar-content-container";
    const mount = document.createElement("div");
    content.append(mount);
    host.append(content);
    const central = document.createElement("div");
    central.className = "p-splitterpanel";
    const canvas = document.createElement("div");
    canvas.className = "graph-canvas-panel";
    setHorizontalBounds(canvas, 233, 1280);
    central.append(canvas);
    splitter.append(host, central);
    document.body.append(splitter);
    const previousInnerWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1280,
    });
    const scheduled: Array<() => void> = [];

    const dispose = createSidebarWidthController(mount, {
      schedule: (callback) => {
        scheduled.push(callback);
        return scheduled.length - 1;
      },
      cancel: () => undefined,
    });
    expect(host.style.width).toBe("704px");
    expect(host.style.flexBasis).toBe("704px");

    // PrimeVue lays the graph panel out beside the widened owner. The remainder is not the
    // available surface by itself: host + canvas still span the same capable viewport.
    setHorizontalBounds(host, 0, 704);
    setHorizontalBounds(canvas, 704, 1280);
    scheduled[0]?.();
    expect(host.style.minWidth).toBe("704px");
    expect(host.style.width).toBe("704px");
    expect(host.style.flexBasis).toBe("704px");
    expect(content.style.minWidth).toBe("704px");
    expect(mount.style.minWidth).toBe("704px");

    dispose();
    splitter.remove();
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: previousInnerWidth,
    });
  });

  it("clamps a host owner that already exceeds the opposite-panel available space", () => {
    const splitter = document.createElement("div");
    splitter.className = "p-splitter";
    splitter.dataset.pcName = "splitter";
    const host = document.createElement("div");
    host.className = "p-splitterpanel side-bar-panel";
    host.style.width = "800px";
    host.style.flexBasis = "800px";
    setHorizontalBounds(host, 0, 800);
    const mount = document.createElement("div");
    host.append(mount);
    const opposite = document.createElement("div");
    opposite.className = "p-splitterpanel side-bar-panel";
    setHorizontalBounds(opposite, 600, 1000);
    const canvas = document.createElement("div");
    canvas.dataset.h3ContextCanvas = "true";
    setHorizontalBounds(canvas, 0, 1000);
    const central = document.createElement("div");
    central.className = "p-splitterpanel";
    central.append(canvas);
    splitter.append(host, central, opposite);
    document.body.append(splitter);
    const previousInnerWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1000,
    });
    const dispose = createSidebarWidthController(mount, {
      schedule: () => 0,
      cancel: () => undefined,
    });
    expect(host.style.width).toBe("600px");
    expect(host.style.flexBasis).toBe("600px");
    dispose();
    expect(host.style.width).toBe("800px");
    expect(host.style.flexBasis).toBe("800px");
    splitter.remove();
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: previousInnerWidth,
    });
  });

  it("TOPOLOGY-02: ignores the central canvas and unrelated panel but clamps a sidebar opposite", () => {
    const splitter = document.createElement("div");
    splitter.className = "p-splitter";
    splitter.dataset.pcName = "splitter";
    const host = document.createElement("div");
    host.className = "p-splitterpanel side-bar-panel";
    host.style.width = "800px";
    host.style.flexBasis = "800px";
    setHorizontalBounds(host, 0, 800);
    const mount = document.createElement("div");
    host.append(mount);
    const central = document.createElement("div");
    central.className = "p-splitterpanel";
    setHorizontalBounds(central, 900, 1000);
    const canvas = document.createElement("div");
    canvas.className = "graph-canvas-panel";
    central.append(canvas);
    const unrelatedRegion = document.createElement("div");
    unrelatedRegion.dataset.h3UnrelatedRegion = "true";
    const unrelated = document.createElement("div");
    unrelated.className = "side-bar-panel";
    setHorizontalBounds(unrelated, 600, 1000);
    unrelatedRegion.append(unrelated);
    // The central canvas is a direct splitter child; the unrelated panel is outside direct
    // splitter-panel topology. Neither is the opposite sidebar owner.
    splitter.append(host, central, unrelatedRegion);
    document.body.append(splitter);

    let dispose: (() => void) | undefined;
    try {
      // A visible panel with a different host topology is not the opposite splitter owner.
      dispose = createSidebarWidthController(mount, {
        schedule: () => 0,
        cancel: () => undefined,
      });
      expect(host.style.width).toBe("800px");
      dispose();
      dispose = undefined;
      unrelatedRegion.remove();

      const opposite = document.createElement("div");
      opposite.className = "p-splitterpanel side-bar-panel";
      setHorizontalBounds(opposite, 600, 1000);
      splitter.append(opposite);
      dispose = createSidebarWidthController(mount, {
        schedule: () => 0,
        cancel: () => undefined,
      });
      expect(host.style.width).toBe("600px");
    } finally {
      dispose?.();
      splitter.remove();
    }
  });

  it("TOPOLOGY-03: ignores a canvas-bearing splitter panel in the p-splitter fallback", () => {
    const splitter = document.createElement("div");
    splitter.className = "p-splitter";
    splitter.dataset.pcName = "splitter";
    const host = document.createElement("div");
    host.className = "p-splitterpanel";
    host.style.width = "800px";
    host.style.flexBasis = "800px";
    setHorizontalBounds(host, 0, 800);
    const mount = document.createElement("div");
    host.append(mount);
    const central = document.createElement("div");
    central.className = "p-splitterpanel";
    setHorizontalBounds(central, 600, 1000);
    const canvas = document.createElement("div");
    canvas.id = "graph-canvas";
    canvas.className = "graph-canvas-panel";
    central.append(canvas);
    const opposite = document.createElement("div");
    opposite.className = "p-splitterpanel";
    setHorizontalBounds(opposite, 700, 1000);
    splitter.append(host, central, opposite);
    document.body.append(splitter);

    let dispose: (() => void) | undefined;
    try {
      dispose = createSidebarWidthController(mount, {
        schedule: () => 0,
        cancel: () => undefined,
      });
      expect(host.style.width).toBe("700px");
    } finally {
      dispose?.();
      splitter.remove();
    }
  });

  it("reflows on a measured mount resize and disconnects without late writes", () => {
    const host = document.createElement("div");
    host.className = "side-bar-panel";
    host.style.width = "320px";
    host.style.flexBasis = "320px";
    setWidth(host, 320);
    const mount = document.createElement("div");
    host.append(mount);
    document.body.append(host);
    const scheduled: Array<() => void> = [];
    const observedTargets: Element[] = [];
    let observerCallback: ResizeObserverCallback | undefined;
    let disconnectCount = 0;
    const previousResizeObserver = globalThis.ResizeObserver;
    class FakeResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        observerCallback = callback;
      }
      observe(target: Element): void {
        observedTargets.push(target);
      }
      disconnect(): void {
        disconnectCount += 1;
      }
      unobserve(): void {
        // The controller owns disconnect; this fake keeps the public shape complete.
      }
    }
    globalThis.ResizeObserver =
      FakeResizeObserver as unknown as typeof ResizeObserver;

    const dispose = createSidebarWidthController(mount, {
      schedule: (callback) => {
        scheduled.push(callback);
        return scheduled.length - 1;
      },
      cancel: () => undefined,
    });

    expect(observedTargets).toContain(mount);
    expect(observedTargets).toContain(host);
    expect(observerCallback).toBeTypeOf("function");
    expect(host.style.width).toBe("704px");
    expect(scheduled).toHaveLength(1);

    host.style.width = "640px";
    host.style.flexBasis = "640px";
    setWidth(host, 640);
    observerCallback?.([], {} as ResizeObserver);
    scheduled[0]?.();
    expect(host.style.width).toBe("704px");

    dispose();
    expect(disconnectCount).toBe(1);
    setWidth(host, 240);
    observerCallback?.([], {} as ResizeObserver);
    scheduled[1]?.();
    expect(host.style.width).toBe("320px");
    globalThis.ResizeObserver = previousResizeObserver;
  });

  it("stops an observer callback when the mounted node is detached", () => {
    const host = document.createElement("div");
    host.className = "side-bar-panel";
    host.style.minWidth = "12px";
    host.style.width = "280px";
    host.style.flexBasis = "280px";
    setWidth(host, 280);
    const mount = document.createElement("div");
    mount.style.minWidth = "24px";
    host.append(mount);
    document.body.append(host);
    const scheduled: Array<() => void> = [];
    let observerCallback: ResizeObserverCallback | undefined;
    let disconnectCount = 0;
    const previousResizeObserver = globalThis.ResizeObserver;
    class FakeResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        observerCallback = callback;
      }
      observe(): void {
        // The controller owns the target; this fake only drives callbacks.
      }
      disconnect(): void {
        disconnectCount += 1;
      }
    }
    globalThis.ResizeObserver =
      FakeResizeObserver as unknown as typeof ResizeObserver;

    createSidebarWidthController(mount, {
      schedule: (callback) => {
        scheduled.push(callback);
        return scheduled.length - 1;
      },
      cancel: () => undefined,
    });
    expect(mount.style.minWidth).toBe("704px");
    scheduled[0]?.();
    mount.remove();
    observerCallback?.([], {} as ResizeObserver);
    scheduled[1]?.();
    expect(disconnectCount).toBe(1);
    expect(mount.style.minWidth).toBe("24px");
    globalThis.ResizeObserver = previousResizeObserver;
  });

  it("clamps the minimum request to the available viewport width", () => {
    const host = document.createElement("div");
    host.className = "side-bar-panel";
    host.style.width = "240px";
    host.style.flexBasis = "240px";
    setWidth(host, 240);
    const mount = document.createElement("div");
    host.append(mount);
    document.body.append(host);
    const previousInnerWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 320,
    });
    const dispose = createSidebarWidthController(mount, {
      schedule: () => 0,
      cancel: () => undefined,
    });
    expect(mount.style.minWidth).toBe("320px");
    expect(host.style.minWidth).toBe("320px");
    expect(host.style.width).toBe("320px");
    expect(host.style.flexBasis).toBe("320px");
    dispose();
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: previousInnerWidth,
    });
  });

  it("restores inline priorities and every touched owner after observer setup fails", () => {
    const host = document.createElement("div");
    host.className = "p-splitterpanel";
    host.style.setProperty("min-width", "12px", "important");
    host.style.setProperty("width", "240px", "important");
    host.style.setProperty("flex-basis", "auto", "important");
    setWidth(host, 240);
    const mount = document.createElement("div");
    host.append(mount);
    document.body.append(host);
    const previousResizeObserver = globalThis.ResizeObserver;
    class ThrowingResizeObserver {
      constructor() {
        throw new Error("observer unavailable");
      }
    }
    globalThis.ResizeObserver =
      ThrowingResizeObserver as unknown as typeof ResizeObserver;

    expect(() => createSidebarWidthController(mount)).toThrow(
      "observer unavailable",
    );
    expect(host.style.getPropertyValue("min-width")).toBe("12px");
    expect(host.style.getPropertyPriority("min-width")).toBe("important");
    expect(host.style.getPropertyValue("width")).toBe("240px");
    expect(host.style.getPropertyPriority("width")).toBe("important");
    expect(host.style.getPropertyValue("flex-basis")).toBe("auto");
    expect(host.style.getPropertyPriority("flex-basis")).toBe("important");
    expect(mount.style.minWidth).toBe("");
    globalThis.ResizeObserver = previousResizeObserver;
  });

  it("mutates only the exact mount and closest host owners, then restores inline values", () => {
    const host = document.createElement("div");
    host.className = "side-bar-panel";
    host.style.minWidth = "12px";
    host.style.width = "280px";
    host.style.flexBasis = "280px";
    setWidth(host, 280);
    const content = document.createElement("div");
    content.className = "sidebar-content-container";
    content.style.minWidth = "18px";
    const mount = document.createElement("div");
    mount.style.minWidth = "24px";
    const unrelated = document.createElement("div");
    unrelated.className = "side-bar-panel";
    unrelated.style.minWidth = "9px";
    host.append(content);
    content.append(mount);
    document.body.append(host, unrelated);
    const scheduled: Array<() => void> = [];
    const cancel = vi.fn();

    const dispose = createSidebarWidthController(mount, {
      schedule: (callback) => {
        scheduled.push(callback);
        return scheduled.length - 1;
      },
      cancel: (handle) => cancel(handle),
    });

    expect(MIN_SIDEBAR_WIDTH_PX).toBe(704);
    expect(mount.style.minWidth).toBe("704px");
    expect(content.style.minWidth).toBe("704px");
    expect(host.style.minWidth).toBe("704px");
    expect(host.style.width).toBe("704px");
    expect(host.style.flexBasis).toBe("704px");
    expect(unrelated.style.minWidth).toBe("9px");

    dispose();
    dispose();
    expect(cancel).toHaveBeenCalledOnce();
    scheduled[0]?.();
    expect(mount.style.minWidth).toBe("24px");
    expect(content.style.minWidth).toBe("18px");
    expect(host.style.minWidth).toBe("12px");
    expect(host.style.width).toBe("280px");
    expect(host.style.flexBasis).toBe("280px");
    expect(unrelated.style.minWidth).toBe("9px");
  });

  it("measures the narrow PrimeVue owner before min-width reflow masks its stale basis", () => {
    const host = document.createElement("div");
    host.className = "p-splitterpanel side-bar-panel";
    host.style.minWidth = "11px";
    host.style.width = "233px";
    host.style.flexBasis = "17px";
    setStyleAwareWidth(host, 233);
    const content = document.createElement("div");
    content.className = "sidebar-content-container";
    content.style.minWidth = "13px";
    content.style.width = "233px";
    setStyleAwareWidth(content, 233);
    const mount = document.createElement("div");
    mount.style.minWidth = "19px";
    content.append(mount);
    host.append(content);
    document.body.append(host);

    const dispose = createSidebarWidthController(mount, {
      schedule: () => 0,
      cancel: () => undefined,
    });

    expect(host.style.minWidth).toBe("704px");
    expect(host.style.width).toBe("704px");
    expect(host.style.flexBasis).toBe("704px");
    expect(content.style.minWidth).toBe("704px");
    // The floor reaches the wrapper through min-width alone; its own width stays the host's.
    expect(content.style.width).toBe("233px");
    expect(content.getBoundingClientRect().width).toBe(704);
    expect(mount.style.minWidth).toBe("704px");

    dispose();
    dispose();
    expect(host.style.minWidth).toBe("11px");
    expect(host.style.width).toBe("233px");
    expect(host.style.flexBasis).toBe("17px");
    expect(content.style.minWidth).toBe("13px");
    expect(content.style.width).toBe("233px");
    expect(mount.style.minWidth).toBe("19px");
  });

  it("preserves a user panel and content wrapper already wider than the product floor", () => {
    const host = document.createElement("div");
    host.className = "p-splitterpanel side-bar-panel";
    host.style.width = "800px";
    host.style.flexBasis = "800px";
    setStyleAwareWidth(host, 800);
    const content = document.createElement("div");
    content.className = "sidebar-content-container";
    content.style.width = "800px";
    setStyleAwareWidth(content, 800);
    const mount = document.createElement("div");
    content.append(mount);
    host.append(content);
    document.body.append(host);

    const dispose = createSidebarWidthController(mount, {
      schedule: () => 0,
      cancel: () => undefined,
    });

    expect(host.style.minWidth).toBe("704px");
    expect(host.style.width).toBe("800px");
    expect(host.style.flexBasis).toBe("800px");
    expect(content.style.minWidth).toBe("704px");
    expect(content.style.width).toBe("800px");
    dispose();
  });

  it("lets the content wrapper follow a host panel dragged past the floor", () => {
    // A PrimeVue splitter drag rewrites the panel's flex-basis; the host sizes the content wrapper
    // from the panel (`size-full`). Model both, so a width H3 writes on the wrapper stays visible.
    let dragged = 233;
    const px = (value: string): number | undefined =>
      value.endsWith("px") ? Number.parseFloat(value) : undefined;
    const layout = (element: HTMLElement, width: () => number): void => {
      Object.defineProperty(element, "getBoundingClientRect", {
        configurable: true,
        value: () => {
          const measured = Math.max(
            width(),
            Number.parseFloat(element.style.minWidth) || 0,
          );
          return {
            bottom: 0,
            height: 0,
            left: 0,
            right: measured,
            toJSON: () => undefined,
            top: 0,
            width: measured,
            x: 0,
            y: 0,
          };
        },
      });
    };
    const host = document.createElement("div");
    host.className = "p-splitterpanel side-bar-panel";
    layout(host, () => px(host.style.flexBasis) ?? dragged);
    const content = document.createElement("div");
    content.className = "sidebar-content-container";
    layout(
      content,
      () => px(content.style.width) ?? host.getBoundingClientRect().width,
    );
    const mount = document.createElement("div");
    layout(
      mount,
      () => px(mount.style.width) ?? content.getBoundingClientRect().width,
    );
    content.append(mount);
    host.append(content);
    document.body.append(host);
    const scheduled: Array<() => void> = [];
    let observerCallback: ResizeObserverCallback | undefined;
    const previousResizeObserver = globalThis.ResizeObserver;
    class FakeResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        observerCallback = callback;
      }
      observe(): void {
        // Geometry changes are delivered by calling the captured callback.
      }
      disconnect(): void {
        // Nothing to release in the fake.
      }
      unobserve(): void {
        // The controller owns disconnect; this fake keeps the public shape complete.
      }
    }
    globalThis.ResizeObserver =
      FakeResizeObserver as unknown as typeof ResizeObserver;
    const settle = (): void => {
      observerCallback?.([], {} as ResizeObserver);
      while (scheduled.length > 0) scheduled.shift()?.();
    };

    try {
      const dispose = createSidebarWidthController(mount, {
        schedule: (callback) => {
          scheduled.push(callback);
          return scheduled.length;
        },
        cancel: () => undefined,
      });
      settle();
      expect(host.getBoundingClientRect().width).toBe(704);
      expect(content.getBoundingClientRect().width).toBe(704);
      expect(mount.getBoundingClientRect().width).toBe(704);

      host.style.flexBasis = "calc(53.9495% - 4px)";
      dragged = 1004;
      settle();
      expect(host.getBoundingClientRect().width).toBe(1004);
      expect(content.style.width).toBe("");
      expect(content.getBoundingClientRect().width).toBe(1004);
      expect(mount.getBoundingClientRect().width).toBe(1004);

      host.style.flexBasis = "calc(20% - 4px)";
      dragged = 380;
      settle();
      expect(host.getBoundingClientRect().width).toBe(704);
      expect(content.getBoundingClientRect().width).toBe(704);
      expect(mount.getBoundingClientRect().width).toBe(704);

      dispose();
      expect(content.style.width).toBe("");
      expect(content.style.minWidth).toBe("");
      expect(mount.style.minWidth).toBe("");
    } finally {
      globalThis.ResizeObserver = previousResizeObserver;
      host.remove();
    }
  });

  it("restores all writes when scheduling the retry fails", () => {
    const host = document.createElement("div");
    host.className = "p-splitterpanel";
    host.style.minWidth = "";
    host.style.width = "240px";
    host.style.flexBasis = "auto";
    setWidth(host, 240);
    const mount = document.createElement("div");
    host.append(mount);
    document.body.append(host);

    expect(() =>
      createSidebarWidthController(mount, {
        schedule: () => {
          throw new Error("scheduler unavailable");
        },
        cancel: () => undefined,
      }),
    ).toThrow("scheduler unavailable");
    expect(host.style.minWidth).toBe("");
    expect(host.style.width).toBe("240px");
    expect(host.style.flexBasis).toBe("auto");
    expect(mount.style.minWidth).toBe("");
  });

  it("captures a host owner that appears on the one scheduled retry", () => {
    const mount = document.createElement("div");
    document.body.append(mount);
    const scheduled: Array<() => void> = [];
    const dispose = createSidebarWidthController(mount, {
      schedule: (callback) => {
        scheduled.push(callback);
        return scheduled.length - 1;
      },
      cancel: () => undefined,
    });
    expect(mount.style.minWidth).toBe("704px");
    const host = document.createElement("div");
    host.className = "p-splitterpanel";
    host.style.minWidth = "11px";
    host.style.width = "250px";
    host.style.flexBasis = "250px";
    setWidth(host, 250);
    document.body.append(host);
    host.append(mount);
    scheduled[0]?.();
    expect(host.style.minWidth).toBe("704px");
    expect(host.style.width).toBe("704px");
    expect(host.style.flexBasis).toBe("704px");
    dispose();
    expect(host.style.minWidth).toBe("11px");
    expect(host.style.width).toBe("250px");
    expect(host.style.flexBasis).toBe("250px");
  });
});
