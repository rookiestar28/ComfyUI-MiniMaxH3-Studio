// The timeline toolbar re-renders in both renders of an accepted edit, because its tool decisions
// follow the authoring state. An icon action whose label, state and description are unchanged must
// still bail out: its handler is a committed delegate and its group's context value is stable. The
// only context an icon action reads is its group's, so counting context reads counts action renders.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { StrictMode, Suspense, startTransition, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const probe = vi.hoisted(() => ({ contextReads: 0 }));

vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useContext: ((context: Parameters<typeof actual.useContext>[0]) => {
      probe.contextReads++;
      return actual.useContext(context);
    }) as typeof actual.useContext,
  };
});

import {
  NleTimelineToolbar,
  type NleTimelineToolbarProps,
} from "../src/components/nle/NleTimelineToolbar";
import type { TimelineToolDecision } from "../src/components/nle/nleTimelineTools";

afterEach(() => {
  cleanup();
  probe.contextReads = 0;
});

const enabled = (): TimelineToolDecision => ({
  enabled: true,
  reason: null,
  commands: [],
});

function props(): NleTimelineToolbarProps {
  return {
    locale: "en",
    decisions: {
      undo: enabled(),
      redo: enabled(),
      split: enabled(),
      trim_start: enabled(),
      trim_end: enabled(),
      delete: enabled(),
      rebase: enabled(),
    },
    rippleEnabled: false,
    snapEnabled: true,
    zoom: { position: 500, canOut: true, canIn: true, status: "Zoom 1x" },
    onTool: vi.fn(),
    onToggleRipple: vi.fn(),
    onToggleSnap: vi.fn(),
    onZoomOut: vi.fn(),
    onZoomIn: vi.fn(),
    onZoomFit: vi.fn(),
    onZoomPosition: vi.fn(),
    overflow: <span>More</span>,
  };
}

describe("timeline toolbar icon actions", () => {
  it("skip an edit's render when only their owner's handlers are new", () => {
    const first = props();
    const view = render(<NleTimelineToolbar {...first} />);
    const settled = probe.contextReads;
    expect(settled).toBeGreaterThan(0);

    // An edit hands the toolbar new handlers and new, equal decision and zoom objects.
    const next = { ...props(), overflow: <span>More</span> };
    view.rerender(<NleTimelineToolbar {...next} />);
    expect(probe.contextReads).toBe(settled);

    // A decision that changes still renders exactly its own action.
    view.rerender(
      <NleTimelineToolbar
        {...next}
        decisions={{
          ...next.decisions,
          split: {
            enabled: false,
            reason: "history_unavailable",
            commands: [],
          },
        }}
      />,
    );
    expect(probe.contextReads).toBe(settled + 1);
    expect(
      view.container
        .querySelector('[data-h3-nle-control="clip.split"]')!
        .getAttribute("aria-disabled"),
    ).toBe("true");
  });

  it("dispatch through the newest committed handlers", () => {
    const first = props();
    const view = render(<NleTimelineToolbar {...first} />);
    const next = props();
    view.rerender(<NleTimelineToolbar {...next} />);
    const control = (id: string) =>
      view.container.querySelector<HTMLElement>(
        `[data-h3-nle-control="${id}"]`,
      )!;
    fireEvent.click(control("history.undo"));
    fireEvent.click(control("clip.remove"));
    fireEvent.click(control("transport.snap"));
    fireEvent.click(control("transport.zoom_in"));
    expect(next.onTool).toHaveBeenNthCalledWith(1, "undo");
    expect(next.onTool).toHaveBeenNthCalledWith(2, "delete");
    expect(next.onToggleSnap).toHaveBeenCalledOnce();
    expect(next.onZoomIn).toHaveBeenCalledOnce();
    expect(first.onTool).not.toHaveBeenCalled();
    expect(first.onToggleSnap).not.toHaveBeenCalled();
    expect(first.onZoomIn).not.toHaveBeenCalled();
  });

  it("never dispatch through an abandoned render's handlers", async () => {
    const first = props();
    const next = props();
    let release!: () => void;
    let allowed = false;
    const pending = new Promise<void>((resolve) => {
      release = () => {
        allowed = true;
        resolve();
      };
    });
    function Gate({ hold }: { hold: boolean }) {
      if (hold && !allowed) throw pending;
      return null;
    }
    let choose = (_next: NleTimelineToolbarProps) => {};
    function Harness() {
      const [current, setCurrent] = useState(first);
      choose = setCurrent;
      return (
        <Suspense fallback={<p>Waiting</p>}>
          <NleTimelineToolbar {...current} />
          <Gate hold={current === next} />
        </Suspense>
      );
    }
    const view = render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    );
    act(() => {
      startTransition(() => choose(next));
    });
    const undo = () =>
      view.container.querySelector<HTMLElement>(
        '[data-h3-nle-control="history.undo"]',
      )!;
    fireEvent.click(undo());
    expect(first.onTool).toHaveBeenCalledExactlyOnceWith("undo");
    expect(next.onTool).not.toHaveBeenCalled();
    await act(async () => {
      release();
    });
    fireEvent.click(undo());
    expect(next.onTool).toHaveBeenCalledExactlyOnceWith("undo");
    expect(first.onTool).toHaveBeenCalledOnce();
  });
});
