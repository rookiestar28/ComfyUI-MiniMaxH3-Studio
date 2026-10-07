import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { NleOverlay } from "../src/components/nle/NleOverlay";
import { seamReady, seamUnavailable } from "../src/host/hostSeams";
import { bindingFixture, expandedState } from "./support/nleWorkspaceBinding";

afterEach(cleanup);

function fixture() {
  const binding = bindingFixture();
  const release = vi.fn();
  const acquire = vi.fn((_onInvalidated: () => void) => seamReady({ release }));
  return {
    binding: {
      ...binding,
      actions: { ...binding.actions, acquireKeyboardGuard: acquire },
    },
    release,
    acquire,
  };
}

it("acquires once across presentation and action-wrapper changes, then releases on a retained compact render", () => {
  const subject = fixture();
  const opening = {
    ...subject.binding,
    state: expandedState({
      surface: { ...subject.binding.state.surface, status: "opening" },
    }),
  };
  const view = render(<NleOverlay binding={opening} />);
  expect(subject.acquire).toHaveBeenCalledOnce();
  expect(subject.binding.actions.mounted).toHaveBeenCalledOnce();
  view.rerender(
    <NleOverlay
      binding={{ ...subject.binding, actions: { ...subject.binding.actions } }}
    />,
  );
  expect(subject.acquire).toHaveBeenCalledOnce();
  expect(subject.release).not.toHaveBeenCalled();
  view.rerender(
    <NleOverlay
      binding={{
        ...subject.binding,
        state: expandedState({
          surface: {
            ...subject.binding.state.surface,
            status: "compact_ready",
          },
        }),
      }}
    />,
  );
  expect(subject.release).toHaveBeenCalledOnce();
  expect(document.querySelector("[data-h3-nle-root]")).toBeNull();
  view.unmount();
  expect(subject.release).toHaveBeenCalledOnce();
});

it("keeps the lease while closing, releases on disposal, and never acquires a compact presentation", () => {
  const subject = fixture();
  const compact = {
    ...subject.binding,
    state: expandedState({
      surface: { ...subject.binding.state.surface, status: "compact_ready" },
    }),
  };
  const view = render(<NleOverlay binding={compact} />);
  expect(subject.acquire).not.toHaveBeenCalled();
  view.rerender(<NleOverlay binding={subject.binding} />);
  expect(subject.acquire).toHaveBeenCalledOnce();
  view.rerender(
    <NleOverlay
      binding={{
        ...subject.binding,
        state: expandedState({
          surface: {
            ...subject.binding.state.surface,
            status: "closing",
            lastCloseReason: "explicit_close",
          },
        }),
      }}
    />,
  );
  expect(subject.release).not.toHaveBeenCalled();
  view.rerender(
    <NleOverlay
      binding={{
        ...subject.binding,
        state: expandedState({
          surface: { ...subject.binding.state.surface, status: "disposed" },
        }),
      }}
    />,
  );
  expect(subject.release).toHaveBeenCalledOnce();
});

it("refuses attachment/focus exactly once when acquisition is unavailable", () => {
  const subject = fixture();
  const acquire = vi.fn(() =>
    seamUnavailable("canvas_keyboard_guard_unavailable"),
  );
  const binding = {
    ...subject.binding,
    state: expandedState({
      surface: { ...subject.binding.state.surface, status: "opening" },
    }),
    actions: { ...subject.binding.actions, acquireKeyboardGuard: acquire },
  };
  render(<NleOverlay binding={binding} />);
  expect(acquire).toHaveBeenCalledOnce();
  expect(binding.actions.mountFailed).toHaveBeenCalledOnce();
  expect(binding.actions.mounted).not.toHaveBeenCalled();
  expect(document.querySelector("[data-h3-nle-root]")).toBeNull();
});

it("contains all bubble phases after React handlers and keeps native defaults", () => {
  const subject = fixture();
  const view = render(<NleOverlay binding={subject.binding} />);
  const foreign = vi.fn();
  window.addEventListener("keydown", foreign);
  window.addEventListener("keyup", foreign);
  window.addEventListener("keypress", foreign);
  try {
    const close = view.getByRole("button", { name: "Close full editor" });
    const event = new KeyboardEvent("keydown", {
      key: "x",
      bubbles: true,
      cancelable: true,
    });
    close.dispatchEvent(event);
    fireEvent.keyUp(close, { key: "x" });
    fireEvent.keyPress(close, { key: "x", charCode: 120 });
    expect(foreign).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
    fireEvent.keyDown(close, { key: "Escape" });
    expect(subject.binding.actions.close).toHaveBeenCalledWith("escape");
  } finally {
    window.removeEventListener("keydown", foreign);
    window.removeEventListener("keyup", foreign);
    window.removeEventListener("keypress", foreign);
  }
});

it("routes lifetime invalidation to the current generation's failure action", () => {
  const subject = fixture();
  const view = render(<NleOverlay binding={subject.binding} />);
  expect(subject.acquire).toHaveBeenCalledOnce();
  const failure = vi.fn();
  view.rerender(
    <NleOverlay
      binding={{
        ...subject.binding,
        actions: { ...subject.binding.actions, mountFailed: failure },
      }}
    />,
  );
  subject.acquire.mock.calls[0]![0]();
  expect(failure).toHaveBeenCalledWith(
    subject.binding.state.surface.generation,
  );
  expect(subject.binding.actions.mountFailed).not.toHaveBeenCalled();
});
