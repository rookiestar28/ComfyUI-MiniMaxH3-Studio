// M25-62: the redesigned clip, grips, help text, empty lane, playhead layer and scroll bar as
// NleTimeline wires them (A62-3..A62-7). jsdom has no layout and no `matchMedia`, so this is the
// fine-pointer DOM contract; the rendered boxes and both pointer modes are proven in the browser
// by `m25_62TimelineSurface.spec.ts`.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleTimeline } from "../src/components/nle/NleTimeline";
import type { PublicCompositionSnapshot } from "../src/contracts/compositionCodec";
import { authoringReady, SMOKE_SHAPE } from "./support/nleWorkspaceFixture";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const HELP =
  "Arrow keys adjust the edge by one frame; Shift+Arrow by one grid step; Enter commits; Escape cancels.";

function subject(
  selection: readonly string[] = ["clip-0"],
  edit?: (snapshot: PublicCompositionSnapshot) => PublicCompositionSnapshot,
  options: Readonly<{
    onSeek?: (frame: number) => void;
    transportAvailable?: boolean;
    navigationAvailable?: boolean;
    strict?: boolean;
  }> = {},
) {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  const authoring = authoringReady(SMOKE_SHAPE, { selection: [...selection] });
  if (authoring.status !== "ready" || !authoring.timelineHistory)
    throw new Error("fixture must be ready");
  const snapshot = edit
    ? edit(authoring.timelineHistory.snapshot)
    : authoring.timelineHistory.snapshot;
  const timeline = (
    <div className="h3-nle-dialog">
      <NleTimeline
        locale="en"
        snapshot={snapshot}
        selection={selection}
        authoring={authoring}
        gridFrames={1}
        playheadFrame={0}
        transportAvailable={options.transportAvailable}
        navigationAvailable={options.navigationAvailable}
        onSeek={options.onSeek}
        highlightedAssetIds={[]}
        onIntent={vi.fn(async () => undefined)}
        onEdgeGestureActive={vi.fn()}
      />
    </div>
  );
  const view = render(
    options.strict ? <StrictMode>{timeline}</StrictMode> : timeline,
  );
  return { view, snapshot };
}

/** Fit maps the 120 s fixture onto jsdom's 120 px minimum lane: every clip is at the 20 px floor. */
function zoomToFit(container: HTMLElement) {
  fireEvent.click(
    container.querySelector('[data-h3-nle-control="transport.zoom_fit"]')!,
  );
}

function zoomToMax(container: HTMLElement) {
  fireEvent.change(
    container.querySelector(
      '[data-h3-nle-control="transport.zoom_continuous"]',
    )!,
    { target: { value: "1000" } },
  );
}

const clip = (container: HTMLElement, id: string) =>
  container.querySelector<HTMLElement>(`[data-h3-nle-clip="${id}"]`)!;

describe("M25-55 persistent logical position", () => {
  it("keeps single-click selection separate and seeks exactly once on an unmodified double-click", () => {
    const onSeek = vi.fn();
    const { view } = subject(["clip-0"], undefined, { onSeek });
    const body = clip(view.container, "clip-0").querySelector<HTMLElement>(
      ".h3-nle-clip-body",
    )!;

    fireEvent.click(body, { clientX: 7, button: 0 });
    expect(onSeek).not.toHaveBeenCalled();
    fireEvent.doubleClick(body, { clientX: 12, button: 0 });
    expect(onSeek).toHaveBeenCalledExactlyOnceWith(12);

    onSeek.mockClear();
    fireEvent.doubleClick(body, { clientX: 18, button: 0, shiftKey: true });
    expect(onSeek).not.toHaveBeenCalled();
  });

  it("keeps logical navigation enabled when preview transport is unavailable", () => {
    const onSeek = vi.fn();
    subject(["clip-0"], undefined, {
      onSeek,
      transportAvailable: false,
      navigationAvailable: true,
    });
    const ruler = screen.getByRole("slider", { name: "Playhead" });
    expect(ruler.getAttribute("aria-disabled")).toBe("false");
    fireEvent.keyDown(ruler, { key: "ArrowRight" });
    expect(onSeek).toHaveBeenCalledExactlyOnceWith(1);
  });
});

describe("content-fit native timeline navigation", () => {
  it("keeps exactly one live native wheel owner through StrictMode and release", () => {
    const add = vi.spyOn(HTMLElement.prototype, "addEventListener");
    const remove = vi.spyOn(HTMLElement.prototype, "removeEventListener");
    const { view } = subject([], undefined, { strict: true });
    const grid = view.getByRole("grid");
    const owned = (spy: typeof add) =>
      spy.mock.calls.filter(
        ([type], index) =>
          type === "wheel" &&
          (spy.mock.instances[index] as HTMLElement).classList.contains(
            "h3-nle-tracks",
          ),
      ).length;
    expect(owned(add) - owned(remove)).toBe(1);

    const timeline = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-region="timeline"]',
    )!;
    const before = Number(timeline.dataset.h3NlePixelsPerFrame);
    fireEvent(
      grid,
      new WheelEvent("wheel", {
        bubbles: true,
        cancelable: true,
        ctrlKey: true,
        deltaY: -100,
        clientX: 60,
      }),
    );
    expect(Number(timeline.dataset.h3NlePixelsPerFrame)).toBeCloseTo(
      before * Math.SQRT2,
      8,
    );

    view.unmount();
    expect(owned(add) - owned(remove)).toBe(0);
  });

  it("cancels Ctrl/Meta zoom and Shift/Alt pan only on the timeline surface", () => {
    const { view } = subject();
    const timeline = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-region="timeline"]',
    )!;
    const grid = view.getByRole("grid");
    const before = Number(timeline.dataset.h3NlePixelsPerFrame);

    const ctrl = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
      deltaY: -100,
      clientX: 60,
    });
    fireEvent(grid, ctrl);
    expect(ctrl.defaultPrevented).toBe(true);
    expect(Number(timeline.dataset.h3NlePixelsPerFrame)).toBeGreaterThan(
      before,
    );

    const meta = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      metaKey: true,
      deltaY: 100,
      clientX: 60,
    });
    fireEvent(grid, meta);
    expect(meta.defaultPrevented).toBe(true);

    for (const init of [
      { shiftKey: true, deltaY: 100 },
      { altKey: true, deltaX: 100 },
    ]) {
      const pan = new WheelEvent("wheel", {
        bubbles: true,
        cancelable: true,
        ...init,
      });
      fireEvent(grid, pan);
      expect(pan.defaultPrevented).toBe(true);
    }

    const ordinary = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      deltaY: 100,
    });
    fireEvent(grid, ordinary);
    expect(ordinary.defaultPrevented).toBe(false);

    const input = document.createElement("input");
    grid.append(input);
    const editable = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
      deltaY: -100,
    });
    fireEvent(input, editable);
    expect(editable.defaultPrevented).toBe(false);
  });
});

describe("M25-62 clip label strip and accessible name (A62-5)", () => {
  it("names a clip by display name, start-end timecodes and duration, never by id", () => {
    const { view } = subject();
    zoomToMax(view.container);
    const body = clip(view.container, "clip-0").querySelector<HTMLElement>(
      ".h3-nle-clip-body",
    )!;
    const name = body.getAttribute("aria-label")!;
    expect(name).toBe("Clip 01, 00:00:00:00–00:00:02:00, 00:00:02:00");
    expect(body.getAttribute("title")).toBe(name);
    expect(
      clip(view.container, "clip-0").querySelector(".h3-nle-clip-label")!
        .textContent,
    ).toBe("Clip 01");
    expect(
      clip(view.container, "clip-0").querySelector(".h3-nle-clip-duration")!
        .textContent,
    ).toBe("00:00:02:00");
  });

  it("marks each clip's kind so text clips take the text tint", () => {
    const { view } = subject();
    zoomToMax(view.container);
    expect(clip(view.container, "clip-0").dataset.h3NleClipKind).toBe("video");
    expect(clip(view.container, "clip-2").dataset.h3NleClipKind).toBe("image");
    expect(clip(view.container, "clip-3").dataset.h3NleClipKind).toBe("text");
    expect(
      clip(view.container, "clip-3").querySelector(".h3-nle-clip-label")!
        .textContent,
    ).toBe("Title");
  });

  it("drops the label strip below 40 px and the duration below 96 px", () => {
    const { view } = subject();
    zoomToFit(view.container);
    const narrow = clip(view.container, "clip-0");
    expect(narrow.querySelector(".h3-nle-clip-label")).toBeNull();
    expect(narrow.querySelector(".h3-nle-clip-duration")).toBeNull();
  });

  it("keeps raw clip and track ids out of every visible text, name and tooltip in the timeline", () => {
    const { view } = subject();
    zoomToMax(view.container);
    const region = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-region="timeline"]',
    )!;
    const exposed = [
      region.textContent ?? "",
      ...[...region.querySelectorAll("[aria-label],[title]")].flatMap(
        (element) => [
          element.getAttribute("aria-label") ?? "",
          element.getAttribute("title") ?? "",
        ],
      ),
    ].join("\n");
    expect(exposed).not.toMatch(/\b(?:clip|track)-\d+\b/u);
    expect(exposed).not.toMatch(/\[\d+, \d+\)/u);
  });
});

describe("M25-62 grips, rail and help text (A62-4)", () => {
  it("puts both compact grips inside a wide selected clip, described by the hidden help", () => {
    const { view } = subject();
    zoomToMax(view.container);
    const selected = clip(view.container, "clip-0");
    expect(selected.dataset.h3NleInlineGrips).toBe("true");
    const grips = selected.querySelectorAll<HTMLButtonElement>(
      ".h3-nle-grip:not([hidden])",
    );
    expect([...grips].map((grip) => grip.dataset.h3NleTrimEdge)).toEqual([
      "start",
      "end",
    ]);
    expect(grips[0]!.getAttribute("aria-label")).toBe("Trim start of Clip 01");
    expect(grips[1]!.getAttribute("aria-label")).toBe("Trim end of Clip 01");
    for (const grip of grips) {
      expect(grip.getAttribute("aria-describedby")).toBe(
        "h3-nle-trim-instructions",
      );
      expect(grip.getAttribute("title")).toBe(HELP);
    }
    const help = view.container.querySelector("#h3-nle-trim-instructions")!;
    expect(help.classList.contains("h3-nle-vh")).toBe(true);
    expect(help.textContent).toBe(HELP);
    expect(view.container.querySelector(".h3-nle-timeline-footer")).toBeNull();
    expect(
      selected.querySelector('[data-h3-nle-menu-trigger="clip"]'),
    ).not.toBeNull();
    expect(view.container.querySelector(".h3-nle-trim-rail")).toBeNull();
  });

  it("keeps one active grip and the rail below 24 px, without id or interval notation", () => {
    const { view } = subject();
    zoomToFit(view.container);
    const selected = clip(view.container, "clip-0");
    expect(selected.dataset.h3NleInlineGrips).toBe("single");
    expect(selected.hasAttribute("data-h3-nle-trim-clip")).toBe(false);
    expect(
      [
        ...selected.querySelectorAll<HTMLElement>(".h3-nle-grip:not([hidden])"),
      ].map((grip) => grip.dataset.h3NleTrimEdge),
    ).toEqual(["end"]);
    const rail =
      view.container.querySelector<HTMLElement>(".h3-nle-trim-rail")!;
    expect(rail.dataset.h3NleTrimClip).toBe("clip-0");
    const label = rail.querySelector(".h3-nle-rail-label")!.textContent!;
    expect(label).toBe("Clip 01 · 00:00:00:00–00:00:02:00");
    expect(rail.getAttribute("aria-label")).not.toContain("clip-0");
    for (const grip of rail.querySelectorAll(".h3-nle-rail-grip"))
      expect(grip.getAttribute("aria-label")).not.toContain("clip-0");
  });

  it("opens the clip menu from the focused clip body with Shift+F10 and the ContextMenu key", () => {
    const { view } = subject();
    zoomToMax(view.container);
    const body = clip(view.container, "clip-0").querySelector<HTMLElement>(
      ".h3-nle-clip-body",
    )!;
    fireEvent.keyDown(body, { key: "F10", shiftKey: true });
    expect(screen.getByRole("menu", { name: "Clip menu" })).toBeTruthy();
    fireEvent.keyDown(screen.getByRole("menu", { name: "Clip menu" }), {
      key: "Escape",
    });
    fireEvent.keyDown(body, { key: "ContextMenu" });
    expect(screen.getByRole("menu", { name: "Clip menu" })).toBeTruthy();
  });
});

describe("M25-62 timeline surface, playhead layer and scroll bar (A62-3, A62-6)", () => {
  it("holds the ruler, the tracks and the scroll bar in one surface with the playhead layer outside the scroller", () => {
    const { view } = subject();
    const surface = view.container.querySelector<HTMLElement>(
      ".h3-nle-timeline-surface",
    )!;
    expect(surface.querySelector(":scope > .h3-nle-ruler")).not.toBeNull();
    expect(surface.querySelector(":scope > .h3-nle-tracks")).not.toBeNull();
    const playhead = surface.querySelector<HTMLElement>(".h3-nle-playhead")!;
    expect(playhead.closest(".h3-nle-tracks")).toBeNull();
    expect(playhead.closest(".h3-nle-playhead-layer")).not.toBeNull();
    const head = surface.querySelector<HTMLElement>(
      "[data-h3-nle-playhead-head]",
    )!;
    expect(head.getAttribute("aria-hidden")).toBe("true");
    expect(head.closest(".h3-nle-playhead-layer")).not.toBeNull();
  });

  it("shows the scroll bar only when the extent exceeds the view, hidden from assistive technology", () => {
    const { view } = subject();
    const bar = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-control="transport.scroll_bar"]',
    )!;
    expect(bar.getAttribute("aria-hidden")).toBe("true");
    zoomToFit(view.container);
    // Content Fit no longer collapses the separately addressable edit-capacity tail.
    expect(bar.hidden).toBe(false);
    zoomToMax(view.container);
    expect(bar.hidden).toBe(false);
    expect(
      view.container.querySelector('[data-h3-nle-control="transport.scroll"]'),
    ).not.toBeNull();
  });
});

describe("M25-62 empty lane (A62-7)", () => {
  it("draws the drop zone in the Main lane when tracks exist and no clip does", () => {
    const { view } = subject([], (snapshot) => ({ ...snapshot, clips: [] }));
    const zone = view.container.querySelector<HTMLElement>(
      "[data-h3-nle-empty-lane]",
    )!;
    // M25-63: the bin's Add is the "+" on a thumbnail.
    expect(zone.textContent).toBe("Drag media here, or press + on a thumbnail");
    expect(zone.closest('[data-h3-nle-track="track-0"]')).not.toBeNull();
    expect(zone.getAttribute("aria-hidden")).toBeNull();
    expect(view.container.textContent).not.toContain(
      "The timeline has no clips.",
    );
  });

  it("keeps the plain sentence when there is no track to drop into", () => {
    const { view } = subject([], (snapshot) => ({
      ...snapshot,
      clips: [],
      tracks: [],
    }));
    expect(view.container.querySelector("[data-h3-nle-empty-lane]")).toBeNull();
    expect(view.container.textContent).toContain("The timeline has no clips.");
  });
});
