// M25-44: the reference shell renders exactly four regions and three separators, the separators
// follow the window splitter pattern (values, keys, reset), a drag previews locally and commits
// once, and the chrome bar's Export popover owns Escape before the dialog.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleOverlay } from "../src/components/nle/NleOverlay";
import { NleShell } from "../src/components/nle/NleShell";
import {
  DEFAULT_NLE_LAYOUT,
  resolveLayout,
  stepLayout,
} from "../src/runtime/nleLayoutGeometry";
import { bindingFixture, expandedState } from "./support/nleWorkspaceBinding";

const STAGE = { width: 1368, height: 790 };
const restores: (() => void)[] = [];

/** jsdom has no layout: give the shell's stage the reference size. */
function stubStage() {
  for (const [property, value] of [
    ["clientWidth", STAGE.width],
    ["clientHeight", STAGE.height],
  ] as const) {
    const original = Object.getOwnPropertyDescriptor(
      Element.prototype,
      property,
    )!;
    Object.defineProperty(HTMLElement.prototype, property, {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains("h3-nle-stage")
          ? value
          : original.get!.call(this);
      },
    });
    restores.push(
      () =>
        delete (HTMLElement.prototype as unknown as Record<string, unknown>)[
          property
        ],
    );
  }
}

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  for (const restore of restores.splice(0)) restore();
});

function shell(onLayout = vi.fn(), layout = DEFAULT_NLE_LAYOUT) {
  const view = render(
    <NleShell
      locale="en"
      layout={layout}
      onLayout={onLayout}
      bin={<p>bin</p>}
      monitor={<p>monitor</p>}
      inspector={<p>inspector</p>}
      timeline={<p>timeline</p>}
    />,
  );
  return { view, onLayout };
}

describe("M25-44 reference shell", () => {
  it("renders exactly four regions and three separators, and no pane mode", () => {
    const { view } = shell();
    const areas = [
      ...view.container.querySelectorAll<HTMLElement>("[data-h3-nle-area]"),
    ].map((area) => area.dataset.h3NleArea);
    expect(areas).toEqual(["bin", "monitor", "inspector", "timeline"]);
    const separators = view.container.querySelectorAll('[role="separator"]');
    expect(
      [...separators].map((element) =>
        element.getAttribute("data-h3-nle-splitter"),
      ),
    ).toEqual(["bin_monitor", "monitor_inspector", "top_timeline"]);
    expect(view.container.querySelector("[data-pane-mode]")).toBeNull();
    for (const separator of separators) {
      expect(separator.getAttribute("tabindex")).toBe("0");
      expect(separator.getAttribute("aria-label")).toMatch(/^Resize /u);
      const controls = separator.getAttribute("aria-controls")!.split(" ");
      for (const id of controls)
        expect(document.getElementById(id)).not.toBeNull();
      for (const name of ["aria-valuenow", "aria-valuemin", "aria-valuemax"])
        expect(Number.isInteger(Number(separator.getAttribute(name)))).toBe(
          true,
        );
    }
    expect(
      view.container
        .querySelector('[data-h3-nle-splitter="top_timeline"]')!
        .getAttribute("aria-orientation"),
    ).toBe("horizontal");
    expect(
      view.container
        .querySelector('[data-h3-nle-splitter="bin_monitor"]')!
        .getAttribute("aria-orientation"),
    ).toBe("vertical");
  });

  it("commits one layout per key, resets on Enter and double click, and keeps keys from bubbling", () => {
    stubStage();
    const { view, onLayout } = shell();
    const outer = vi.fn();
    // Above React's root container, where a native stopPropagation takes effect.
    document.body.addEventListener("keydown", outer);
    restores.push(() => document.body.removeEventListener("keydown", outer));
    const s1 = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-splitter="bin_monitor"]',
    )!;
    const before = resolveLayout(DEFAULT_NLE_LAYOUT, STAGE);
    expect(Number(s1.getAttribute("aria-valuenow"))).toBe(
      Math.round((before.bin / STAGE.width) * 100),
    );
    fireEvent.keyDown(s1, { key: "ArrowRight", shiftKey: true });
    expect(onLayout).toHaveBeenCalledTimes(1);
    const moved = resolveLayout(onLayout.mock.calls[0]![0], STAGE);
    expect(moved.bin - before.bin).toBe(64);
    fireEvent.keyDown(s1, { key: "Home" });
    fireEvent.keyDown(s1, { key: "End" });
    fireEvent.keyDown(s1, { key: "Enter" });
    expect(onLayout).toHaveBeenCalledTimes(4);
    fireEvent.doubleClick(s1);
    expect(onLayout).toHaveBeenCalledTimes(5);
    fireEvent.keyDown(s1, { key: "a" });
    expect(onLayout).toHaveBeenCalledTimes(5);
    // Handled keys never reach an outer listener; an unhandled key does.
    expect(outer).toHaveBeenCalledTimes(1);
  });

  it("previews a pointer drag locally and commits once on release", () => {
    stubStage();
    const { view, onLayout } = shell();
    const s3 = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-splitter="top_timeline"]',
    )!;
    s3.setPointerCapture = vi.fn();
    s3.hasPointerCapture = vi.fn(() => true);
    s3.releasePointerCapture = vi.fn();
    const shellElement = view.container.querySelector<HTMLElement>(
      "[data-h3-nle-shell]",
    )!;
    const before = resolveLayout(DEFAULT_NLE_LAYOUT, STAGE);
    expect(shellElement.style.getPropertyValue("--h3-nle-top")).toBe(
      `${before.top}px`,
    );
    fireEvent.pointerDown(s3, { pointerId: 7, clientX: 10, clientY: 100 });
    fireEvent.pointerMove(s3, { pointerId: 7, clientX: 10, clientY: 60 });
    expect(onLayout).not.toHaveBeenCalled();
    expect(shellElement.style.getPropertyValue("--h3-nle-top")).toBe(
      `${before.top - 40}px`,
    );
    fireEvent.pointerUp(s3, { pointerId: 7, clientX: 10, clientY: 60 });
    expect(onLayout).toHaveBeenCalledTimes(1);
    expect(resolveLayout(onLayout.mock.calls[0]![0], STAGE).top).toBe(
      before.top - 40,
    );
  });

  it.each([
    ["bin_monitor", "vertical", -1],
    ["bin_monitor", "vertical", 1],
    ["monitor_inspector", "vertical", -1],
    ["monitor_inspector", "vertical", 1],
    ["top_timeline", "horizontal", -1],
    ["top_timeline", "horizontal", 1],
  ] as const)(
    "moves the %s splitter one step by clicking its %s-direction side",
    (id, orientation, direction) => {
      stubStage();
      const { view, onLayout } = shell();
      const splitter = view.container.querySelector<HTMLElement>(
        `[data-h3-nle-splitter="${id}"]`,
      )!;
      splitter.setPointerCapture = vi.fn();
      splitter.hasPointerCapture = vi.fn(() => true);
      splitter.releasePointerCapture = vi.fn();
      const rect =
        orientation === "vertical"
          ? new DOMRect(100, 100, 44, 790)
          : new DOMRect(100, 100, 1368, 44);
      vi.spyOn(splitter, "getBoundingClientRect").mockReturnValue(rect);
      const before = DEFAULT_NLE_LAYOUT;
      const clickOffset = direction < 0 ? 4 : 40;
      const clientX = orientation === "vertical" ? 100 + clickOffset : 120;
      const clientY = orientation === "horizontal" ? 100 + clickOffset : 120;

      fireEvent.pointerDown(splitter, {
        pointerId: 19,
        clientX,
        clientY,
      });
      fireEvent.pointerUp(splitter, {
        pointerId: 19,
        clientX,
        clientY,
      });

      expect(onLayout).toHaveBeenCalledTimes(1);
      expect(onLayout.mock.calls[0]![0]).toEqual(
        stepLayout(before, id, direction, false, STAGE),
      );
    },
  );
});

describe("M25-44 chrome bar", () => {
  it("orders Export before Close, opens the popover and closes it on Escape before the dialog", () => {
    const binding = bindingFixture({ state: expandedState() });
    render(<NleOverlay binding={binding} />);
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
    const exportButton = dialog.querySelector<HTMLButtonElement>(
      '[data-h3-nle-action="export"]',
    )!;
    const close = dialog.querySelector<HTMLButtonElement>(
      '[data-h3-nle-action="close"]',
    )!;
    expect(
      exportButton.compareDocumentPosition(close) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    expect(dialog.querySelector("footer")).toBeNull();
    const popover = dialog.querySelector<HTMLElement>(
      '[data-h3-nle-popover="export"]',
    )!;
    expect(popover.hidden).toBe(true);
    // The render hooks stay mounted while the popover is closed.
    expect(popover.querySelector("[data-h3-nle-render]")).not.toBeNull();
    act(() => exportButton.click());
    expect(popover.hidden).toBe(false);
    expect(exportButton.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(popover, { key: "Escape" });
    expect(popover.hidden).toBe(true);
    expect(document.activeElement).toBe(exportButton);
    expect(binding.actions.close).not.toHaveBeenCalled();
    fireEvent.keyDown(exportButton, { key: "Escape" });
    expect(binding.actions.close).toHaveBeenCalledWith("escape");
  });

  it("closes the popover on a press outside it", () => {
    const binding = bindingFixture({ state: expandedState() });
    render(<NleOverlay binding={binding} />);
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
    const exportButton = dialog.querySelector<HTMLButtonElement>(
      '[data-h3-nle-action="export"]',
    )!;
    act(() => exportButton.click());
    const popover = dialog.querySelector<HTMLElement>(
      '[data-h3-nle-popover="export"]',
    )!;
    fireEvent.pointerDown(popover);
    expect(popover.hidden).toBe(false);
    fireEvent.pointerDown(dialog.querySelector("h2")!);
    expect(popover.hidden).toBe(true);
  });
});
