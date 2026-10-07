// M25-45 B-M2545-31: the transport's Escape guard while the monitor owns the screen. The guard's
// whole purpose is that Escape leaves full screen and goes no further -- it must not also reach the
// dialog behind the screen -- so what matters is that it is armed for every Escape that can arrive
// while the document is full screen on the owned element, including the first one.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createRef } from "react";

import { NleTransport } from "../src/components/nle/NleTransport";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  Reflect.deleteProperty(document, "fullscreenElement");
  Reflect.deleteProperty(document, "exitFullscreen");
});

/** Puts the document into full screen on `element` the way the browser does: the property first. */
function enterFullscreen(element: HTMLElement) {
  Object.defineProperty(document, "fullscreenElement", {
    configurable: true,
    value: element,
  });
}

function mountTransport() {
  const target = createRef<HTMLElement>();
  const host = document.createElement("section");
  document.body.append(host);
  // The ref the monitor passes is its own R2 section, which exists before the transport mounts.
  (target as { current: HTMLElement | null }).current = host;
  const exitFullscreen = vi.fn(() => Promise.resolve());
  Object.defineProperty(document, "exitFullscreen", {
    configurable: true,
    value: exitFullscreen,
  });
  render(
    <NleTransport
      locale="en"
      playing={false}
      controlsAvailable
      currentFrame={0}
      lastFrame={47}
      fps={24}
      view="fit"
      onView={() => undefined}
      onPlay={() => undefined}
      onPause={() => undefined}
      onStep={() => undefined}
      fullscreenTarget={target}
    />,
  );
  return { host, exitFullscreen };
}

describe("the transport's full-screen Escape guard", () => {
  it("leaves full screen on the first Escape, before any fullscreenchange is mirrored", () => {
    const { host, exitFullscreen } = mountTransport();
    // The browser has granted full screen. React has not yet been told: `fullscreenchange` has not
    // been dispatched, so nothing mirrored into state can have happened. This is the real window --
    // `requestFullscreen` resolves and the event lands a task later, and a user (or a test) can
    // press Escape inside it.
    enterFullscreen(host);

    const event = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(event);

    expect(exitFullscreen).toHaveBeenCalledOnce();
    // ...and it goes no further: the dialog behind the screen must not see this key.
    expect(event.defaultPrevented).toBe(true);
  });

  it("leaves full screen on Escape once the change has been mirrored", () => {
    const { host, exitFullscreen } = mountTransport();
    enterFullscreen(host);
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });

    fireEvent.keyDown(document, { key: "Escape" });

    expect(exitFullscreen).toHaveBeenCalledOnce();
  });

  it("does not consume Escape when this monitor does not own the screen", () => {
    const { exitFullscreen } = mountTransport();
    const foreign = document.createElement("div");
    document.body.append(foreign);
    enterFullscreen(foreign);

    const event = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(event);

    expect(exitFullscreen).not.toHaveBeenCalled();
    // The dialog's own Escape must still reach it, or a monitor that owns nothing would trap the key.
    expect(event.defaultPrevented).toBe(false);
  });

  it("does not consume Escape when nothing is full screen", () => {
    const { exitFullscreen } = mountTransport();

    const event = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(event);

    expect(exitFullscreen).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });
});
