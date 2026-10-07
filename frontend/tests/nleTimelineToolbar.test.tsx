import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleTimelineToolbar } from "../src/components/nle/NleTimelineToolbar";
import {
  resolveTimelineShortcut,
  type TimelineToolDecision,
} from "../src/components/nle/nleTimelineTools";

afterEach(cleanup);

const enabled: TimelineToolDecision = {
  enabled: true,
  reason: null,
  commands: [],
};
const disabled: TimelineToolDecision = {
  enabled: false,
  reason: "history_unavailable",
  commands: [],
};

function subject(overrides: Record<string, unknown> = {}) {
  const onTool = vi.fn();
  const view = render(
    <NleTimelineToolbar
      locale="en"
      decisions={{
        undo: enabled,
        redo: disabled,
        split: enabled,
        trim_start: enabled,
        trim_end: enabled,
        delete: enabled,
        rebase: disabled,
      }}
      rippleEnabled={false}
      snapEnabled={true}
      zoom={{ position: 500, canOut: true, canIn: true, status: "Zoom 1x" }}
      onTool={onTool}
      onToggleRipple={vi.fn()}
      onToggleSnap={vi.fn()}
      onZoomOut={vi.fn()}
      onZoomIn={vi.fn()}
      onZoomFit={vi.fn()}
      onZoomPosition={vi.fn()}
      overflow={
        <button type="button" data-h3-nle-alternative="selection.next">
          Next clip
        </button>
      }
      {...overrides}
    />,
  );
  return { ...view, onTool };
}

describe("M25-47 timeline toolbar", () => {
  it("renders one named toolbar in one bounded row with every primary control", () => {
    const view = subject();
    const toolbar = view.getByRole("toolbar", { name: "Timeline tools" });
    expect(toolbar.querySelectorAll("[data-h3-nle-toolbar-item]")).toHaveLength(
      12,
    );
    for (const id of [
      "history.undo",
      "history.redo",
      "clip.split",
      "clip.trim_start_playhead",
      "clip.trim_end_playhead",
      "clip.remove",
      "transport.snap",
      "transport.ripple",
      "transport.zoom_out",
      "transport.zoom_in",
      "transport.zoom_fit",
      "toolbar.more",
    ])
      expect(
        toolbar.querySelector(`[data-h3-nle-control="${id}"]`),
      ).not.toBeNull();
    expect(toolbar.querySelectorAll("*").length).toBeLessThanOrEqual(60);
  });

  it("M25-62: splits the same order into an edit group and a view group with CSS-only separators", () => {
    const view = subject();
    const toolbar = view.getByRole("toolbar", { name: "Timeline tools" });
    const breaks = [
      ...toolbar.querySelectorAll<HTMLElement>("[data-h3-nle-toolbar-break]"),
    ].map((slot) => [
      slot.querySelector<HTMLElement>("[data-h3-nle-control]")?.dataset
        .h3NleControl ??
        slot.dataset.h3NleToolbarBreakFor ??
        "",
      slot.dataset.h3NleToolbarBreak,
    ]);
    expect(breaks).toEqual([
      ["clip.split", "separator"],
      ["transport.snap", "group"],
      ["transport.zoom_out", "separator"],
      ["toolbar.more", "separator"],
    ]);
    // Separators are pseudo-elements: no element is added for them.
    expect(toolbar.querySelectorAll("[role=separator], hr")).toHaveLength(0);
    expect(toolbar.querySelectorAll("*").length).toBeLessThanOrEqual(60);
  });

  it("M25-62: names each available shortcut in the tooltip and in aria-keyshortcuts", () => {
    const view = subject();
    const expected: Record<string, [string, string]> = {
      "history.undo": ["Ctrl+Z", "Control+Z"],
      "history.redo": ["Ctrl+Shift+Z", "Control+Shift+Z"],
      "clip.split": ["Ctrl+B", "Control+B"],
      "clip.trim_start_playhead": ["Q", "Q"],
      "clip.trim_end_playhead": ["W", "W"],
      "clip.remove": ["Del", "Delete"],
      "transport.zoom_out": ["Ctrl+-", "Control+-"],
      "transport.zoom_in": ["Ctrl+=", "Control+="],
      "transport.zoom_fit": ["Shift+Z", "Shift+Z"],
    };
    for (const [control, [shown, aria]] of Object.entries(expected)) {
      const button = view.container.querySelector<HTMLElement>(
        `[data-h3-nle-control="${control}"]`,
      )!;
      expect(button.getAttribute("aria-keyshortcuts"), control).toBe(aria);
      fireEvent.focus(button);
      expect(view.getByRole("tooltip").textContent, control).toContain(
        `(${shown})`,
      );
      fireEvent.blur(button);
    }
    for (const control of [
      "transport.snap",
      "transport.ripple",
      "toolbar.more",
    ])
      expect(
        view.container
          .querySelector(`[data-h3-nle-control="${control}"]`)!
          .hasAttribute("aria-keyshortcuts"),
      ).toBe(false);
  });

  // The toolbar's shortcut table is a display copy of `resolveTimelineShortcut`, which owns the
  // keys. Every advertised shortcut must reach that control's own action through the resolver, so
  // a changed binding fails here instead of leaving a stale tooltip and `aria-keyshortcuts`.
  it("M25-62: every advertised shortcut resolves to its own control's action", () => {
    const actions: Record<string, string> = {
      "history.undo": "undo",
      "history.redo": "redo",
      "clip.split": "split",
      "clip.trim_start_playhead": "trim_start",
      // With ripple on, the same button and Q run the same tool, which then ripple-trims.
      "range.ripple_trim": "trim_start",
      "clip.trim_end_playhead": "trim_end",
      "clip.remove": "delete",
      "range.ripple_delete": "ripple_delete",
      "transport.zoom_out": "zoom_out",
      "transport.zoom_in": "zoom_in",
      "transport.zoom_fit": "zoom_fit",
    };
    const seen = new Set<string>();
    for (const rippleEnabled of [false, true]) {
      const view = subject({ rippleEnabled });
      for (const button of view.container.querySelectorAll<HTMLElement>(
        "[aria-keyshortcuts]",
      )) {
        const control = button.dataset.h3NleControl!;
        const tokens = button.getAttribute("aria-keyshortcuts")!.split("+");
        const key = tokens.at(-1)!;
        const resolved = resolveTimelineShortcut({
          key,
          ctrlKey: tokens.includes("Control"),
          metaKey: false,
          shiftKey: tokens.includes("Shift"),
          altKey: false,
          repeat: false,
          isComposing: false,
          defaultPrevented: false,
          target: null,
        });
        expect(resolved?.action, control).toBe(actions[control]);
        seen.add(control);
      }
      cleanup();
    }
    expect([...seen].sort()).toEqual(Object.keys(actions).sort());
  });

  it("M25-62: names the ripple shortcut when Delete becomes ripple delete", () => {
    const view = subject({ rippleEnabled: true });
    const button = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-control="range.ripple_delete"]',
    )!;
    expect(button.getAttribute("aria-keyshortcuts")).toBe("Shift+Delete");
    fireEvent.focus(button);
    expect(view.getByRole("tooltip").textContent).toContain("(Shift+Del)");
  });

  it("keeps unavailable actions focusable with a reason while refusing every activation", () => {
    const view = subject();
    const redo = view.getByRole("button", { name: "Redo" });
    expect(redo.getAttribute("aria-disabled")).toBe("true");
    expect(redo.hasAttribute("disabled")).toBe(false);
    fireEvent.focus(redo);
    fireEvent.click(redo);
    expect(view.onTool).not.toHaveBeenCalledWith("redo");
    expect(view.getByRole("tooltip").textContent).toContain("No redo history");
  });

  it("uses roving focus with arrows and Home/End without stealing range keys", () => {
    const view = subject();
    const undo = view.getByRole("button", { name: "Undo" });
    const redo = view.getByRole("button", { name: "Redo" });
    undo.focus();
    fireEvent.keyDown(undo, { key: "ArrowRight" });
    expect(document.activeElement).toBe(redo);
    fireEvent.keyDown(redo, { key: "End" });
    expect((document.activeElement as HTMLElement).dataset.h3NleControl).toBe(
      "toolbar.more",
    );
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(document.activeElement).toBe(undo);

    const slider = view.getByRole("slider", { name: "Timeline zoom" });
    slider.focus();
    fireEvent.keyDown(slider, { key: "ArrowRight" });
    expect(document.activeElement).toBe(slider);
  });

  it("changes Delete to the canonical ripple operation without duplicating an id", () => {
    const view = subject({ rippleEnabled: true });
    expect(
      view.container.querySelectorAll(
        '[data-h3-nle-control="range.ripple_delete"]',
      ),
    ).toHaveLength(1);
    expect(
      view.container.querySelector('[data-h3-nle-control="clip.remove"]'),
    ).toBeNull();
    fireEvent.click(view.getByRole("button", { name: "Ripple delete" }));
    expect(view.onTool).toHaveBeenCalledWith("delete");
  });

  it("closes overflow on Escape and restores focus before the overlay can own the key", () => {
    const view = subject();
    const more = view.getByRole("button", { name: "More timeline tools" });
    fireEvent.click(more);
    const menu = view.getByRole("menu", { name: "More timeline tools" });
    expect(menu.hasAttribute("hidden")).toBe(false);
    const handled = fireEvent.keyDown(menu, { key: "Escape" });
    expect(handled).toBe(false);
    expect(
      view.queryByRole("menu", { name: "More timeline tools" }),
    ).toBeNull();
    expect(document.activeElement).toBe(more);
  });

  it("moves focus to the stable toolbar before dispatching a rebase that hides its banner", () => {
    const view = subject({
      decisions: {
        undo: enabled,
        redo: disabled,
        split: enabled,
        trim_start: enabled,
        trim_end: enabled,
        delete: enabled,
        rebase: enabled,
      },
      showConflict: true,
    });
    const rebase = view.getByRole("button", { name: "Rebase rejected edit" });
    const undo = view.getByRole("button", { name: "Undo" });
    rebase.focus();
    fireEvent.click(rebase);
    expect(document.activeElement).toBe(undo);
    expect(view.onTool).toHaveBeenCalledWith("rebase");
  });
});
