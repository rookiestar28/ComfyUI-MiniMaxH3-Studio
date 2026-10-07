// M25-16 structural budgets: the smoke and virtualized fixtures render at most eight track
// rows, at most 96 clip nodes and at most 1500 DOM nodes inside the workspace, whatever the
// zoom, and every clip the window admits is reachable by scrolling rather than dropped.

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { NleWorkspace } from "../src/components/nle/NleWorkspace";
import {
  NLE_MAX_CLIP_NODES,
  NLE_MAX_VISIBLE_ROWS,
} from "../src/components/nle/NleTimeline";
import {
  SMOKE_SHAPE,
  VIRTUALIZED_SHAPE,
  authoringReady,
  snapshotFixture,
  type FixtureShape,
} from "./support/nleWorkspaceFixture";
import {
  availableDisposition,
  bindingFixture,
  expandedState,
} from "./support/nleWorkspaceBinding";

const NLE_MAX_DOM_NODES = 1500;

// jsdom lays nothing out; pretend the lane is very wide and tall enough for the full row cap so
// every clip and every row would be admitted without the budget.
const layout: { width: number; height: number } = { width: 8000, height: 4000 };

function defineLayout() {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() {
      return layout.width;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get() {
      return layout.height;
    },
  });
}

function undefineLayout() {
  delete (HTMLElement.prototype as unknown as Record<string, unknown>)[
    "clientWidth"
  ];
  delete (HTMLElement.prototype as unknown as Record<string, unknown>)[
    "clientHeight"
  ];
}

function renderShape(shape: FixtureShape) {
  const binding = bindingFixture({
    authoring: authoringReady(shape),
    runtime: availableDisposition(),
    state: {
      ...expandedState({}, { width: 1280, height: 800 }),
      render: { status: "read", capability: null },
    },
  });
  const view = render(
    <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />,
  );
  return { binding, view };
}

function counts(container: HTMLElement) {
  return {
    rows: container.querySelectorAll('[role="row"]').length,
    clips: container.querySelectorAll("[data-h3-nle-clip]").length,
    dom: container.querySelectorAll("*").length,
  };
}

beforeEach(defineLayout);
afterEach(() => {
  cleanup();
  undefineLayout();
});

describe("M25-16 workspace structural budgets", () => {
  it("renders the smoke fixture (120 s, 4 tracks, 64 clips) inside every budget", () => {
    const { view } = renderShape(SMOKE_SHAPE);
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    expect(snapshot.clips).toHaveLength(64);
    // Zoom fully out so the whole 120 s composition is inside the lane.
    const zoomOut = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="transport.zoom_out"]',
    )!;
    fireEvent.click(zoomOut);
    fireEvent.click(zoomOut);
    const measured = counts(view.container);
    expect(measured.rows).toBe(4);
    expect(measured.clips).toBe(64);
    expect(measured.clips).toBeLessThanOrEqual(NLE_MAX_CLIP_NODES);
    expect(measured.dom).toBeLessThanOrEqual(NLE_MAX_DOM_NODES);
  });

  it("renders the virtualized fixture (600 s, 8 tracks, 128 clips) inside every budget", () => {
    const { view } = renderShape(VIRTUALIZED_SHAPE);
    const snapshot = snapshotFixture(VIRTUALIZED_SHAPE);
    expect(snapshot.clips).toHaveLength(128);
    expect(snapshot.tracks).toHaveLength(8);
    const zoomOut = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="transport.zoom_out"]',
    )!;
    fireEvent.click(zoomOut);
    fireEvent.click(zoomOut);
    const measured = counts(view.container);
    expect(measured.rows).toBe(NLE_MAX_VISIBLE_ROWS);
    expect(measured.clips).toBe(NLE_MAX_CLIP_NODES);
    expect(measured.dom).toBeLessThanOrEqual(NLE_MAX_DOM_NODES);
    expect(
      view.container
        .querySelector("[data-h3-nle-virtual-rows]")!
        .getAttribute("data-h3-nle-virtual-rows"),
    ).toBe(String(NLE_MAX_VISIBLE_ROWS));
  });

  it("keeps the row window at the cap even when the viewport could show more rows", () => {
    layout.height = 56 * 40;
    const { view } = renderShape(VIRTUALIZED_SHAPE);
    expect(counts(view.container).rows).toBeLessThanOrEqual(
      NLE_MAX_VISIBLE_ROWS,
    );
    layout.height = 4000;
  });

  it("windows rows by scroll position so later tracks become reachable", () => {
    layout.height = 56 * 2;
    const { view } = renderShape(VIRTUALIZED_SHAPE);
    const scroll = view.container.querySelector<HTMLElement>(
      "[data-h3-nle-virtual-rows]",
    )!;
    const before = [...view.container.querySelectorAll('[role="row"]')].map(
      (row) => row.getAttribute("data-h3-nle-track"),
    );
    expect(before.length).toBeLessThan(8);
    Object.defineProperty(scroll, "scrollTop", {
      configurable: true,
      value: 56 * 5,
    });
    fireEvent.scroll(scroll);
    const after = [...view.container.querySelectorAll('[role="row"]')].map(
      (row) => row.getAttribute("data-h3-nle-track"),
    );
    expect(after).not.toEqual(before);
    expect(after).toContain("track-7");
    layout.height = 4000;
  });

  it("windows clips by the visible frame range at the default zoom", () => {
    layout.width = 1600;
    const { view } = renderShape(VIRTUALIZED_SHAPE);
    const measured = counts(view.container);
    expect(measured.clips).toBeGreaterThan(0);
    expect(measured.clips).toBeLessThan(128);
    // Scrolling the lane to the end reveals the last clips.
    const scroll = view.container.querySelector<HTMLInputElement>(
      '[data-h3-nle-control="transport.scroll"]',
    )!;
    expect(scroll.type).toBe("range");
    fireEvent.change(scroll, { target: { value: scroll.max } });
    const ids = [...view.container.querySelectorAll("[data-h3-nle-clip]")].map(
      (clip) => clip.getAttribute("data-h3-nle-clip"),
    );
    expect(ids).toContain("clip-127");
    expect(ids.length).toBeLessThanOrEqual(NLE_MAX_CLIP_NODES);
    layout.width = 8000;
  });
});
