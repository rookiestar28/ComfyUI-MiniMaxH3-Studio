import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  NleTimeline,
  type NleTimelineProps,
  type NleTimelineShortcutHandler,
  type NleTimelineTransportSnapshot,
} from "../src/components/nle/NleTimeline";
import { authoringReady, SMOKE_SHAPE } from "./support/nleWorkspaceFixture";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function props(): NleTimelineProps {
  const authoring = authoringReady(SMOKE_SHAPE, { selection: ["clip-0"] });
  if (authoring.status !== "ready" || !authoring.timelineHistory)
    throw new Error("ready timeline required");
  return {
    locale: "en",
    snapshot: authoring.timelineHistory.snapshot,
    selection: ["clip-0"],
    authoring,
    gridFrames: 1,
    playheadFrame: 0,
    highlightedAssetIds: [],
    onIntent: vi.fn(async () => undefined),
    onEdgeGestureActive: vi.fn(),
  };
}

function transport(frame: number) {
  let value: NleTimelineTransportSnapshot = {
    frame,
    request: null,
    settledRequestGeneration: 0,
    transportAvailable: true,
    navigationAvailable: true,
  };
  const listeners = new Set<() => void>();
  return {
    listeners,
    channel: {
      snapshot: () => value,
      subscribe: (listener: () => void) => {
        listeners.add(listener);
        return () => {
          listeners.delete(listener);
        };
      },
    },
    publish(next: number) {
      value = { ...value, frame: next };
      for (const listener of listeners) listener();
    },
  };
}

function shortcuts() {
  const handlers = new Set<NleTimelineShortcutHandler>();
  const bind: NonNullable<NleTimelineProps["bindShortcuts"]> = (handler) => {
    if (handler !== null) handlers.add(handler);
    return () => {
      if (handler !== null) handlers.delete(handler);
    };
  };
  return { handlers, bind };
}

describe("timeline facade lifecycle", () => {
  it.each(["pointer cancellation", "navigation loss", "unmount"] as const)(
    "closes its pending scrub RAF on %s and ignores a late callback",
    (ending) => {
      vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
      class OwnedPointerEvent extends MouseEvent {
        readonly pointerId = 7;
        readonly isPrimary = true;
      }
      vi.stubGlobal("PointerEvent", OwnedPointerEvent);
      let nextRaf = 0;
      const pending = new Map<number, FrameRequestCallback>();
      vi.stubGlobal(
        "requestAnimationFrame",
        (callback: FrameRequestCallback) => {
          pending.set(++nextRaf, callback);
          return nextRaf;
        },
      );
      vi.stubGlobal("cancelAnimationFrame", (id: number) => pending.delete(id));
      const input = props();
      const seek = vi.fn();
      const tree = (navigationAvailable = true) => (
        <StrictMode>
          <NleTimeline
            {...input}
            navigationAvailable={navigationAvailable}
            onSeek={seek}
          />
        </StrictMode>
      );
      const view = render(tree());
      const ruler = screen.getByRole("slider", { name: "Playhead" });
      const captured = new Set<number>();
      ruler.setPointerCapture = (id) => {
        captured.add(id);
      };
      ruler.hasPointerCapture = (id) => captured.has(id);
      ruler.releasePointerCapture = (id) => {
        captured.delete(id);
      };
      const baseline = new Set(pending.keys());
      fireEvent.pointerDown(ruler, { button: 0, clientX: 240 });
      const scrubIds = [...pending.keys()].filter((id) => !baseline.has(id));
      expect(scrubIds).toHaveLength(1);
      const scrubId = scrubIds[0]!;
      const lateCallback = pending.get(scrubId)!;
      expect(seek).not.toHaveBeenCalled();
      if (ending === "pointer cancellation") {
        fireEvent.pointerCancel(ruler);
        expect(seek).toHaveBeenCalledExactlyOnceWith(0);
        expect(captured.size).toBe(0);
      } else if (ending === "navigation loss") {
        view.rerender(tree(false));
        expect(ruler.getAttribute("aria-disabled")).toBe("true");
        expect(seek).not.toHaveBeenCalled();
      } else {
        view.unmount();
        expect(seek).not.toHaveBeenCalled();
      }
      expect(pending.has(scrubId)).toBe(false);
      const calls = seek.mock.calls.length;
      act(() => lateCallback(16));
      expect(seek).toHaveBeenCalledTimes(calls);
      view.unmount();
      expect(pending.size).toBe(0);
    },
  );

  it("balances its measured viewport observer through StrictMode, pointer mode changes and release", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    const observers = new Set<MeasuredObserver>();
    class MeasuredObserver {
      readonly targets = new Set<Element>();
      constructor() {
        observers.add(this);
      }
      observe(target: Element) {
        this.targets.add(target);
      }
      disconnect() {
        this.targets.clear();
      }
    }
    vi.stubGlobal("ResizeObserver", MeasuredObserver);
    const changes = new Set<() => void>();
    const query = {
      matches: false,
      addEventListener: (_type: string, change: () => void) =>
        changes.add(change),
      removeEventListener: (_type: string, change: () => void) =>
        changes.delete(change),
    };
    vi.stubGlobal("matchMedia", () => query);
    const input = props();
    const view = render(
      <StrictMode>
        <NleTimeline {...input} />
      </StrictMode>,
    );
    const active = () =>
      [...observers].filter((observer) => observer.targets.size > 0);
    expect(active()).toHaveLength(1);
    expect(active()[0]!.targets.has(screen.getByRole("grid"))).toBe(true);
    expect(changes.size).toBe(1);
    const previous = active()[0];
    act(() => {
      query.matches = true;
      for (const change of changes) change();
    });
    expect(previous!.targets.size).toBe(0);
    expect(active()).toHaveLength(1);
    expect(
      view.container.querySelector<HTMLElement>(
        "[data-h3-nle-region='timeline']",
      )!.dataset.h3NleLaneOriginPx,
    ).toBe("176");
    view.unmount();
    expect(active()).toHaveLength(0);
    expect(changes.size).toBe(0);
  });

  it("keeps one transport and shortcut owner on rebind, with current frame decisions and balanced release", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    const first = transport(3);
    const second = transport(5);
    const firstKeys = shortcuts();
    const secondKeys = shortcuts();
    const seek = vi.fn();
    const input = props();
    const tree = (
      port: ReturnType<typeof transport>,
      keys: ReturnType<typeof shortcuts>,
    ) => (
      <StrictMode>
        <div onKeyDown={(event) => keys.handlers.forEach((key) => key(event))}>
          <NleTimeline
            {...input}
            transportChannel={port.channel}
            bindShortcuts={keys.bind}
            onSeek={seek}
          />
        </div>
      </StrictMode>
    );
    const view = render(tree(first, firstKeys));
    expect(first.listeners.size).toBe(1);
    expect(firstKeys.handlers.size).toBe(1);
    const ruler = screen.getByRole("slider", { name: "Playhead" });
    expect(ruler.getAttribute("aria-valuenow")).toBe("3");
    view.rerender(tree(second, secondKeys));
    expect(first.listeners.size).toBe(0);
    expect(firstKeys.handlers.size).toBe(0);
    expect(second.listeners.size).toBe(1);
    expect(secondKeys.handlers.size).toBe(1);
    act(() => second.publish(11));
    expect(ruler.getAttribute("aria-valuenow")).toBe("11");
    act(() => first.publish(99));
    expect(ruler.getAttribute("aria-valuenow")).toBe("11");
    fireEvent.keyDown(ruler, { key: "ArrowRight" });
    expect(seek).toHaveBeenCalledExactlyOnceWith(12);
    fireEvent.keyDown(screen.getByRole("grid"), { key: "b", ctrlKey: true });
    expect(input.onIntent).toHaveBeenCalledOnce();
    expect(vi.mocked(input.onIntent).mock.calls[0]![0]).toMatchObject({
      action: "apply_timeline_commands",
      commands: [{ kind: "split_clip" }],
    });
    view.unmount();
    expect(second.listeners.size).toBe(0);
    expect(secondKeys.handlers.size).toBe(0);
  });
});
