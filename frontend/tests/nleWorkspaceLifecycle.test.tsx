// M25-16 overlay lifecycle: the session state machine (open, mounted, close reasons, mount
// failure, capability refusal, view destroy, resize clamp) and the `overlay_v1` root (heading
// focus, Escape edge-first, close button, Tab trap, resize handle, focus return, mount failure).

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NleLauncher } from "../src/components/nle/NleLauncher";
import {
  NleOverlay,
  returnFocusAfterClose,
} from "../src/components/nle/NleOverlay";
import { initialNleSurfaceState } from "../src/state/nleWorkspaceState";
import { projectionFixture } from "./support/authoringFixture";
import type { TimelineHistoryProjectionV2 } from "../src/contracts/authoringWorkbenchCodec";
import type { NleWorkspaceBinding } from "../src/components/nle/nleWorkspaceBinding";
import { UNSUPPORTED_OUTPUT_CAPABILITY } from "../src/host/authoringOutputCapabilityClient";
import { createNleWorkspaceSession } from "../src/lifecycle/nleWorkspaceSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  type RuntimeCapabilityObservation,
} from "../src/runtime/mediaCapabilities";
import {
  clampOverlayBounds,
  overlayDefaultBounds,
} from "../src/runtime/nleOverlayGeometry";
import { DEFAULT_NLE_LAYOUT } from "../src/runtime/nleLayoutGeometry";
import {
  availableDisposition,
  bindingFixture,
  expandedState,
} from "./support/nleWorkspaceBinding";

const observation = vi.hoisted(() => ({ current: null as unknown }));

vi.mock("../src/runtime/mediaCapabilities", async (importOriginal) => {
  const original =
    await importOriginal<typeof import("../src/runtime/mediaCapabilities")>();
  return {
    ...original,
    observeBrowserMediaCapabilities: () =>
      (observation.current as RuntimeCapabilityObservation | null) ??
      original.observeBrowserMediaCapabilities(),
  };
});

function supportedObservation(): RuntimeCapabilityObservation {
  return {
    schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    htmlMediaElement: true,
    canPlayMp4H264Aac: "probably",
    requestVideoFrameCallback: true,
    seekedEvent: true,
    timeupdateEvent: true,
    canvas2d: true,
    crossOriginIsolated: false,
  };
}

function unsupportedObservation(): RuntimeCapabilityObservation {
  return {
    ...supportedObservation(),
    htmlMediaElement: false,
    canvas2d: false,
  };
}

function viewport() {
  return { width: window.innerWidth, height: window.innerHeight };
}

function subject(
  options: {
    connected?: boolean;
    capabilityRead?: () => Promise<unknown>;
  } = {},
) {
  const session = createShellSession();
  const container = document.createElement("div");
  if (options.connected !== false) document.body.append(container);
  session.container = container;
  const renderCurrent = vi.fn();
  const selectPage = vi.fn();
  const runAuthoringIntent = vi.fn(async () => undefined);
  const read = vi.fn(
    options.capabilityRead ?? (async () => UNSUPPORTED_OUTPUT_CAPABILITY),
  );
  const runtime = {
    session,
    deps: { authoringOutputCapabilityClient: { read } },
    actions: {
      renderCurrent,
      selectPage,
      runAuthoringIntent,
      mediaRuntimeLeaveContext: vi.fn(),
    },
  } as unknown as ShellRuntime;
  const nle = createNleWorkspaceSession(runtime);
  return {
    nle,
    session,
    renderCurrent,
    selectPage,
    read,
    container,
    runAuthoringIntent,
  };
}

it("loads a newly created compact workspace history once on explicit full-editor open", () => {
  const { nle, session, runAuthoringIntent } = subject();
  session.authoringState = { status: "ready", projection: projectionFixture() };
  nle.nleOpenOverlay();
  nle.nleOpenOverlay();
  expect(runAuthoringIntent).toHaveBeenCalledExactlyOnceWith({
    action: "initialize_timeline_history",
  });
});

it("does not reinitialize an existing empty V2 authoring history on open", () => {
  const { nle, session, runAuthoringIntent } = subject();
  session.authoringState = {
    status: "ready",
    projection: projectionFixture(),
    timelineHistoryV2: {} as TimelineHistoryProjectionV2,
  };
  nle.nleOpenOverlay();
  expect(runAuthoringIntent).not.toHaveBeenCalled();
});

beforeEach(() => {
  observation.current = supportedObservation();
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  observation.current = null;
});

describe("M25-16 NLE workspace session lifecycle", () => {
  it("opens with a fresh generation, default bounds and the assets pane, then expands on mount", async () => {
    const { nle, session, read } = subject();
    expect(session.nleWorkspace.surface.status).toBe("compact_ready");
    expect(nle.nleLauncherCapability()?.capability).toBe("overlay_v1");
    nle.nleOpenOverlay();
    const opening = session.nleWorkspace.surface;
    expect(opening.status).toBe("opening");
    expect(opening.generation).toBe(1);
    expect(opening.pane).toBe("assets");
    expect(opening.bounds).toEqual(overlayDefaultBounds(viewport()));
    expect(opening.lastCloseReason).toBeNull();
    // A repeated open while opening is a no-op: one request, one root.
    nle.nleOpenOverlay();
    expect(session.nleWorkspace.surface.generation).toBe(1);
    nle.nleOverlayMounted(1);
    expect(session.nleWorkspace.surface.status).toBe("expanded");
    await Promise.resolve();
    await Promise.resolve();
    expect(read).toHaveBeenCalledTimes(1);
    expect(session.nleWorkspace.render.status).toBe("read");
    expect(session.nleWorkspace.render.capability).toEqual(
      UNSUPPORTED_OUTPUT_CAPABILITY,
    );
  });

  it("ignores a stale mounted/released report from an older generation", () => {
    const { nle, session } = subject();
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(0);
    expect(session.nleWorkspace.surface.status).toBe("opening");
    nle.nleOverlayMounted(1);
    nle.nleCloseOverlay("explicit_close");
    nle.nleOverlayReleased(0);
    expect(session.nleWorkspace.surface.status).toBe("closing");
    nle.nleOverlayReleased(1);
    expect(session.nleWorkspace.surface.status).toBe("compact_ready");
  });

  it.each([
    "explicit_close",
    "escape",
    "function_switch",
    "top_level_navigation",
  ] as const)(
    "records exactly one close reason (%s) and returns to compact_ready after release",
    (reason) => {
      const { nle, session } = subject();
      nle.nleOpenOverlay();
      nle.nleOverlayMounted(1);
      nle.nleCloseOverlay(reason);
      const closing = session.nleWorkspace.surface;
      expect(closing.status).toBe("closing");
      expect(closing.lastCloseReason).toBe(reason);
      // A second close while closing does not overwrite the recorded reason.
      nle.nleCloseOverlay("escape");
      expect(session.nleWorkspace.surface.lastCloseReason).toBe(reason);
      nle.nleOverlayReleased(1);
      const released = session.nleWorkspace.surface;
      expect(released.status).toBe("compact_ready");
      expect(released.lastCloseReason).toBe(reason);
      expect(released.bounds).toEqual({ width: 0, height: 0 });
      expect(released.generation).toBe(1);
      // Focus returns to the launcher except for function switch and top-level navigation.
      expect(nle.nleFocusKeyForReturn()).toBe(
        reason === "function_switch" || reason === "top_level_navigation"
          ? ""
          : "nle-open-overlay",
      );
      // The next open gets a new generation and clears the reason.
      nle.nleOpenOverlay();
      expect(session.nleWorkspace.surface.generation).toBe(2);
      expect(session.nleWorkspace.surface.lastCloseReason).toBeNull();
    },
  );

  it("refuses to open when the media runtime is unsupported and reports the unavailable status", () => {
    observation.current = unsupportedObservation();
    const { nle, session } = subject();
    expect(nle.nleLauncherCapability()).toBeNull();
    nle.nleOpenOverlay();
    const surface = session.nleWorkspace.surface;
    expect(surface.status).toBe("compact_unsupported");
    expect(surface.generation).toBe(0);
    expect(surface.lastCloseReason).toBe("capability_or_mount_failure");
    expect(surface.capability?.capability).toBe("unsupported");
    expect(surface.capability?.failureDisposition).toBe(
      "media_runtime_unavailable",
    );
  });

  it("refuses to open when the sidebar container is not admitted to the document", () => {
    const { nle, session } = subject({ connected: false });
    nle.nleOpenOverlay();
    expect(session.nleWorkspace.surface.status).toBe("compact_unsupported");
    expect(session.nleWorkspace.surface.capability?.failureDisposition).toBe(
      "mount_admission_refused",
    );
  });

  it("records a mount failure as capability_or_mount_failure and stays compact", () => {
    const { nle, session } = subject();
    nle.nleOpenOverlay();
    nle.nleOverlayMountFailed(1);
    const surface = session.nleWorkspace.surface;
    expect(surface.status).toBe("compact_unsupported");
    expect(surface.lastCloseReason).toBe("capability_or_mount_failure");
    expect(surface.capability?.capability).toBe("unsupported");
    expect(surface.capability?.failureDisposition).toBe(
      "mount_admission_refused",
    );
    // A later good open is still possible once the capability is derived again.
    nle.nleOpenOverlay();
    expect(session.nleWorkspace.surface.status).toBe("opening");
    expect(session.nleWorkspace.surface.generation).toBe(2);
  });

  it("resets geometry and pane on view destroy and records view_destroy once", () => {
    const { nle, session } = subject();
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(1);
    nle.nleSelectPane("sequence");
    nle.nleResizeOverlay({ width: 900, height: 600 });
    nle.nleDisposeOverlay();
    const surface = session.nleWorkspace.surface;
    expect(surface.status).toBe("compact_ready");
    expect(surface.lastCloseReason).toBe("view_destroy");
    expect(surface.pane).toBe("assets");
    expect(surface.bounds).toEqual({ width: 0, height: 0 });
    expect(surface.generation).toBe(1);
    // A destroy while already compact keeps the earlier reason.
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(2);
    nle.nleCloseOverlay("escape");
    nle.nleOverlayReleased(2);
    nle.nleDisposeOverlay();
    expect(session.nleWorkspace.surface.lastCloseReason).toBe("escape");
  });

  it("clamps user resize between the minimum and the available viewport", () => {
    const { nle, session } = subject();
    nle.nleOpenOverlay();
    nle.nleResizeOverlay({ width: 100, height: 100 });
    // Resize is ignored until expanded.
    expect(session.nleWorkspace.surface.bounds).toEqual(
      overlayDefaultBounds(viewport()),
    );
    nle.nleOverlayMounted(1);
    nle.nleResizeOverlay({ width: 100, height: 100 });
    expect(session.nleWorkspace.surface.bounds).toEqual(
      clampOverlayBounds(viewport(), { width: 100, height: 100 }),
    );
    expect(session.nleWorkspace.surface.bounds.width).toBe(720);
    expect(session.nleWorkspace.surface.bounds.height).toBe(480);
    nle.nleResizeOverlay({ width: 10_000, height: 10_000 });
    expect(session.nleWorkspace.surface.bounds).toEqual({
      width: window.innerWidth - 32,
      height: window.innerHeight - 32,
    });
    nle.nleResizeOverlay({
      width: Number.NaN,
      height: Number.POSITIVE_INFINITY,
    });
    expect(session.nleWorkspace.surface.bounds).toEqual(
      overlayDefaultBounds(viewport()),
    );
  });

  it("M25-44 keeps splitter layout as validated session memory across close and reopen", () => {
    const { nle, session } = subject();
    nle.nleOpenOverlay();
    expect(session.nleWorkspace.surface.layout).toEqual(DEFAULT_NLE_LAYOUT);
    const moved = { bin: 0.3, inspector: 0.2, top: 0.5 };
    // A move before the mount settles is ignored, like a resize.
    nle.nleSetLayout(moved);
    expect(session.nleWorkspace.surface.layout).toEqual(DEFAULT_NLE_LAYOUT);
    nle.nleOverlayMounted(1);
    nle.nleSetLayout(moved);
    expect(session.nleWorkspace.surface.layout).toEqual(moved);
    nle.nleCloseOverlay("explicit_close");
    nle.nleOverlayReleased(1);
    nle.nleOpenOverlay();
    expect(session.nleWorkspace.surface.layout).toEqual(moved);
    nle.nleOverlayMounted(2);
    // An invalid share is replaced by its default, never stored.
    nle.nleSetLayout({ bin: Number.NaN, inspector: 2, top: 0.5 });
    expect(session.nleWorkspace.surface.layout).toEqual({
      ...DEFAULT_NLE_LAYOUT,
      top: 0.5,
    });
    expect(JSON.stringify(window.localStorage)).not.toContain("0.3");
  });

  it("lets a reopened generation read its own capability while an older read is outstanding, and ignores the stale one", async () => {
    // Post-closeout finding F2: capability-read ownership is scoped to the overlay generation.
    // A read that outlives close/reopen must neither strand the next generation in `reading` nor
    // overwrite its result when it finally lands.
    const pending: ((value: unknown) => void)[] = [];
    const read = vi.fn(
      () =>
        new Promise((done) => {
          pending.push(done);
        }),
    );
    const { nle, session } = subject({ capabilityRead: read });
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(1);
    expect(session.nleWorkspace.render.status).toBe("reading");
    nle.nleCloseOverlay("explicit_close");
    nle.nleOverlayReleased(1);
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(2);
    // Generation 2 must have started its own read despite generation 1's being outstanding.
    expect(read).toHaveBeenCalledTimes(2);
    expect(session.nleWorkspace.render.status).toBe("reading");
    const stale = { ...UNSUPPORTED_OUTPUT_CAPABILITY, reason: "stale" };
    const current = { ...UNSUPPORTED_OUTPUT_CAPABILITY, reason: "current" };
    // Stale completion first: it cannot settle or clear generation 2's read.
    pending[0]!(stale);
    await Promise.resolve();
    await Promise.resolve();
    expect(session.nleWorkspace.render.status).toBe("reading");
    pending[1]!(current);
    await Promise.resolve();
    await Promise.resolve();
    expect(session.nleWorkspace.render).toEqual({
      status: "read",
      capability: current,
    });
    // A third open after a settled read starts a fresh read; a late generation-1 completion
    // arriving now (already consumed above) has nothing left to touch.
    nle.nleCloseOverlay("explicit_close");
    nle.nleOverlayReleased(2);
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(3);
    expect(read).toHaveBeenCalledTimes(3);
  });

  it("resets capability-read eligibility on view destroy and mount failure", async () => {
    const pending: ((value: unknown) => void)[] = [];
    const read = vi.fn(
      () =>
        new Promise((done) => {
          pending.push(done);
        }),
    );
    const { nle, session } = subject({ capabilityRead: read });
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(1);
    expect(session.nleWorkspace.render.status).toBe("reading");
    nle.nleDisposeOverlay();
    expect(session.nleWorkspace.render.status).not.toBe("reading");
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(2);
    expect(read).toHaveBeenCalledTimes(2);
    pending[0]!({ ...UNSUPPORTED_OUTPUT_CAPABILITY, reason: "stale" });
    await Promise.resolve();
    await Promise.resolve();
    expect(session.nleWorkspace.render.status).toBe("reading");
    nle.nleOverlayMountFailed(2);
    expect(session.nleWorkspace.render.status).not.toBe("reading");
    pending[1]!({ ...UNSUPPORTED_OUTPUT_CAPABILITY, reason: "late" });
    await Promise.resolve();
    await Promise.resolve();
    expect(session.nleWorkspace.render.capability).not.toEqual({
      ...UNSUPPORTED_OUTPUT_CAPABILITY,
      reason: "late",
    });
  });

  it("does not adopt a capability read that lands after the overlay closed", async () => {
    let resolve!: (value: unknown) => void;
    const pending = new Promise((done) => {
      resolve = done;
    });
    const { nle, session } = subject({ capabilityRead: () => pending });
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(1);
    expect(session.nleWorkspace.render.status).toBe("reading");
    nle.nleCloseOverlay("explicit_close");
    nle.nleOverlayReleased(1);
    nle.nleOpenOverlay();
    resolve({ ...UNSUPPORTED_OUTPUT_CAPABILITY });
    await pending;
    await Promise.resolve();
    // The stale result is not adopted, and the closed generation's read no longer blocks the
    // next one: the state returns to `read` with the previously retained (absent) capability.
    expect(session.nleWorkspace.render).toEqual({
      status: "read",
      capability: null,
    });
  });

  it("focuses the clip editor through the production page and a consumed generation", () => {
    const { nle, session, selectPage } = subject();
    expect(session.nleWorkspace.functionRequest.generation).toBe(0);
    nle.nleFocusClipEditor();
    expect(selectPage).toHaveBeenCalledWith("production");
    expect(session.nleWorkspace.functionRequest).toEqual({
      id: "clip_editor",
      generation: 1,
    });
    expect(session.nleWorkspace.surface.status).toBe("compact_ready");
  });
});

function overlayBinding(
  status: "opening" | "expanded" | "closing",
  overrides: Partial<NleWorkspaceBinding> = {},
): NleWorkspaceBinding {
  const state = expandedState({}, { width: 1000, height: 700 });
  return bindingFixture({
    runtime: availableDisposition(),
    state: {
      ...state,
      surface: {
        ...state.surface,
        status,
        lastCloseReason: status === "closing" ? "escape" : null,
      },
      render: { status: "read", capability: null },
    },
    ...overrides,
  });
}

function launcher(): HTMLButtonElement {
  const button = document.createElement("button");
  button.setAttribute("data-h3-focus-key", "nle-open-overlay");
  document.body.append(button);
  return button;
}

const SURFACE = '[data-h3-nle-surface="overlay_v1"]';

describe("M25-16 overlay_v1 root", () => {
  it("keeps the focused owned root attached when shell action wrappers change", () => {
    const binding = overlayBinding("expanded");
    const view = render(<NleOverlay binding={binding} />);
    const root = document.querySelector<HTMLElement>("[data-h3-nle-root]")!;
    const close = root.querySelector<HTMLButtonElement>(
      '[data-h3-nle-action="close"]',
    )!;
    close.focus();
    const remove = vi.spyOn(root, "remove");
    view.rerender(
      <NleOverlay binding={{ ...binding, actions: { ...binding.actions } }} />,
    );
    expect(remove).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(close);
  });

  it("portals an owned h3c root into the body, focuses the heading and reports mounted once", () => {
    const binding = overlayBinding("opening");
    render(<NleOverlay binding={binding} />);
    const root = document.body.querySelector("[data-h3-nle-root]")!;
    expect(root).not.toBeNull();
    expect(root.classList.contains("h3c")).toBe(true);
    expect(root.getAttribute("data-h3-nle-root")).toBe("1");
    const dialog = root.querySelector<HTMLElement>(SURFACE)!;
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("data-h3-nle-state")).toBe("opening");
    expect(dialog.style.width).toBe("1000px");
    expect(dialog.style.height).toBe("700px");
    expect(document.activeElement).toBe(dialog.querySelector("h2"));
    expect(binding.actions.mounted).toHaveBeenCalledTimes(1);
    expect(binding.actions.mounted).toHaveBeenCalledWith(1);
    expect(binding.actions.mountFailed).not.toHaveBeenCalled();
    expect(root.querySelector('[data-h3-nle-status="surface"]')).not.toBeNull();
    // No foreign root is touched.
    expect(document.body.hasAttribute("inert")).toBe(false);
    expect(document.body.getAttribute("aria-hidden")).toBeNull();
  });

  it("closes with escape from the dialog", () => {
    const binding = overlayBinding("expanded");
    render(<NleOverlay binding={binding} />);
    const dialog = document.body.querySelector<HTMLElement>(SURFACE)!;
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(binding.actions.close).toHaveBeenCalledWith("escape");
  });

  it("closes explicitly from the close button", () => {
    const binding = overlayBinding("expanded");
    render(<NleOverlay binding={binding} />);
    fireEvent.click(
      document.body.querySelector('[data-h3-nle-action="close"]')!,
    );
    expect(binding.actions.close).toHaveBeenCalledWith("explicit_close");
  });

  it("traps Tab inside the dialog in both directions", () => {
    // jsdom lays nothing out; give every element a box so the trap sees the real tab order.
    const rects = vi
      .spyOn(Element.prototype, "getClientRects")
      .mockImplementation(
        () => [{ width: 1, height: 1 }] as unknown as DOMRectList,
      );
    const binding = overlayBinding("expanded");
    render(<NleOverlay binding={binding} />);
    const dialog = document.body.querySelector<HTMLElement>(SURFACE)!;
    const enabled = [
      ...dialog.querySelectorAll<HTMLElement>("button:not([disabled])"),
    ];
    const first = enabled[0]!;
    const last = enabled[enabled.length - 1]!;
    last.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    first.focus();
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    // Focus outside the dialog is pulled back in.
    const outside = launcher();
    outside.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    rects.mockRestore();
  });

  it("resizes through the handle with pointer and keyboard and reports every request", () => {
    const binding = overlayBinding("expanded");
    render(<NleOverlay binding={binding} />);
    const handle = document.body.querySelector<HTMLButtonElement>(
      '[data-h3-nle-action="resize"]',
    )!;
    handle.setPointerCapture = vi.fn();
    handle.hasPointerCapture = vi.fn(() => true);
    handle.releasePointerCapture = vi.fn();
    fireEvent.pointerDown(handle, { clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 50, clientY: 30, pointerId: 1 });
    expect(binding.actions.resize).toHaveBeenLastCalledWith({
      width: 1040,
      height: 720,
    });
    fireEvent.pointerUp(handle, { clientX: 50, clientY: 30, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 90, clientY: 90, pointerId: 1 });
    expect(binding.actions.resize).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(binding.actions.resize).toHaveBeenLastCalledWith({
      width: 1016,
      height: 700,
    });
    fireEvent.keyDown(handle, { key: "ArrowUp", shiftKey: true });
    expect(binding.actions.resize).toHaveBeenLastCalledWith({
      width: 1000,
      height: 636,
    });
  });

  it("releases the generation and returns focus to the launcher when closing", () => {
    const target = launcher();
    const binding = overlayBinding("closing");
    render(<NleOverlay binding={binding} />);
    expect(binding.actions.released).toHaveBeenCalledWith(1);
    expect(document.activeElement).toBe(target);
    // The closing view unmounts the workspace: no control remains.
    expect(document.body.querySelector("[data-h3-nle-control]")).toBeNull();
  });

  it("does not return focus on function switch or top-level navigation", () => {
    const target = launcher();
    const state = overlayBinding("closing").state;
    const binding = bindingFixture({
      runtime: availableDisposition(),
      state: {
        ...state,
        surface: { ...state.surface, lastCloseReason: "function_switch" },
      },
    });
    render(<NleOverlay binding={binding} />);
    expect(binding.actions.released).toHaveBeenCalledWith(1);
    expect(document.activeElement).not.toBe(target);
  });

  it("puts the owned root in the top layer when the engine has popovers, and leaves the z-index stacking otherwise", () => {
    // B-M2544-09: a foreign z-index may exceed the backdrop's, so the root enters the top layer.
    const prototype = HTMLElement.prototype as HTMLElement & {
      showPopover?: () => void;
      hidePopover?: () => void;
    };
    const original = {
      showPopover: Object.getOwnPropertyDescriptor(prototype, "showPopover"),
      hidePopover: Object.getOwnPropertyDescriptor(prototype, "hidePopover"),
    };
    const stub = (name: keyof typeof original, value: unknown) =>
      Object.defineProperty(prototype, name, { configurable: true, value });
    const restore = () => {
      for (const name of ["showPopover", "hidePopover"] as const) {
        const descriptor = original[name];
        if (descriptor) Object.defineProperty(prototype, name, descriptor);
        else delete (prototype as unknown as Record<string, unknown>)[name];
      }
    };
    const show = vi.fn();
    const hide = vi.fn();
    stub("showPopover", show);
    stub("hidePopover", hide);
    try {
      const view = render(<NleOverlay binding={overlayBinding("expanded")} />);
      const root =
        document.body.querySelector<HTMLElement>("[data-h3-nle-root]")!;
      expect(root.getAttribute("popover")).toBe("manual");
      expect(show).toHaveBeenCalledTimes(1);
      expect(show.mock.contexts[0]).toBe(root);
      expect(root.style.position).toBe("fixed");
      // Viewport-wide, never collapsed: `.h3c` is the `h3-sidebar` inline-size container.
      expect(root.style.inset).toBe("0px");
      expect(root.style.width).toBe("auto");
      expect(root.style.backgroundColor).toBe("transparent");
      // The body and every other element are untouched.
      expect(document.body.hasAttribute("popover")).toBe(false);
      view.unmount();
      expect(hide).toHaveBeenCalledTimes(1);
      expect(document.body.querySelector("[data-h3-nle-root]")).toBeNull();
    } finally {
      restore();
    }
    // An engine without the Popover API keeps the root unstyled in the z-index stacking.
    stub("showPopover", undefined);
    try {
      const view = render(<NleOverlay binding={overlayBinding("expanded")} />);
      const root =
        document.body.querySelector<HTMLElement>("[data-h3-nle-root]")!;
      expect(root.hasAttribute("popover")).toBe(false);
      expect(root.getAttribute("style")).toBeNull();
      view.unmount();
    } finally {
      restore();
    }
  });

  it("removes the owned root on unmount and renders nothing when compact", () => {
    const binding = overlayBinding("expanded");
    const view = render(<NleOverlay binding={binding} />);
    expect(document.body.querySelector("[data-h3-nle-root]")).not.toBeNull();
    view.unmount();
    expect(document.body.querySelector("[data-h3-nle-root]")).toBeNull();
    const compactState = expandedState();
    const compact = bindingFixture({
      runtime: availableDisposition(),
      state: {
        ...compactState,
        surface: { ...compactState.surface, status: "compact_ready" },
      },
    });
    render(<NleOverlay binding={compact} />);
    expect(document.body.querySelector(SURFACE)).toBeNull();
  });

  it("reports a workspace render failure as a mount failure", () => {
    const expanded = overlayBinding("expanded");
    const binding: NleWorkspaceBinding = {
      ...expanded,
      state: {
        ...expanded.state,
        surface: { ...expanded.state.surface, pane: "sequence" },
      },
    };
    // A binding that throws while the sequence pane renders is a mount failure of the
    // owned workspace, never a host-level crash.
    (
      binding.actions as { sequenceStartable: () => boolean }
    ).sequenceStartable = vi.fn(() => {
      throw new Error("render failure");
    });
    const error = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    try {
      act(() => {
        render(<NleOverlay binding={binding} />);
      });
    } finally {
      error.mockRestore();
    }
    expect(binding.actions.mountFailed).toHaveBeenCalledWith(1);
  });
});

// M25-20 `SidebarEditorUiInvariantV1.overlay_close_return_focus`: one case per non-destroy close
// reason with its exact destination, plus the ordered fallback the policy falls back through.
//
// The pre-existing coverage proved the launcher case and asserted only that a function switch does
// *not* return focus. `top_level_navigation` was named in that test's title without being exercised
// separately, and the fallback chain had no case at all — so a reordering of the target list, or a
// close reason quietly moving between the two halves of the policy, would not have been caught.
describe("M25-20 overlay close return-focus destinations", () => {
  function target(selector: string, value: string): HTMLButtonElement {
    const button = document.createElement("button");
    button.setAttribute(selector, value);
    document.body.append(button);
    return button;
  }

  it.each(["explicit_close", "escape", "capability_or_mount_failure"] as const)(
    "returns focus to the launcher after %s",
    (reason) => {
      const button = target("data-h3-focus-key", "nle-open-overlay");
      returnFocusAfterClose(reason);
      expect(document.activeElement).toBe(button);
    },
  );

  it.each(["function_switch", "top_level_navigation", "view_destroy"] as const)(
    "leaves focus where the new surface put it after %s",
    (reason) => {
      // The destination belongs to whatever the user navigated to; stealing focus back to a
      // launcher the user has just left is the defect this half of the policy exists to avoid.
      const button = target("data-h3-focus-key", "nle-open-overlay");
      returnFocusAfterClose(reason);
      expect(document.activeElement).not.toBe(button);
    },
  );

  it("falls back to the clip editor tab when the launcher is gone", () => {
    const tab = target("data-h3-director-function", "clip_editor");
    target("data-h3-focus-key", "page-production");
    returnFocusAfterClose("escape");
    expect(document.activeElement).toBe(tab);
  });

  it("falls back to the production page when neither the launcher nor the tab is present", () => {
    const page = target("data-h3-focus-key", "page-production");
    returnFocusAfterClose("escape");
    expect(document.activeElement).toBe(page);
  });

  it("moves nothing when no destination is connected", () => {
    const before = document.activeElement;
    returnFocusAfterClose("escape");
    expect(document.activeElement).toBe(before);
  });
});

// M25-20 `overlay_close_return_focus.capability_or_mount_failure` through the real launcher: the
// pure policy above resolved to the launcher, but the launcher itself unmounted on the refusal
// that answered its own click and nothing ran the policy for the synchronous capability
// refusal, so the browser row observed focus dropped on `document.body` (B-63).
describe("M25-20 the launcher survives the refusal that answers its own gesture", () => {
  function launcher(
    surface: typeof initialNleSurfaceState,
    supported: boolean,
    onOpen = () => undefined,
  ) {
    return (
      <NleLauncher
        locale="en"
        surface={surface}
        supported={supported}
        onOpen={onOpen}
      />
    );
  }

  it("keeps the focused launcher mounted and focused after a capability refusal at open", () => {
    const view = render(launcher(initialNleSurfaceState, true));
    const button = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-focus-key="nle-open-overlay"]',
    )!;
    act(() => button.focus());
    expect(document.activeElement).toBe(button);
    // The session's refusal: `compact_unsupported` with this reason, and the capability now
    // observed unsupported on the same render.
    act(() => {
      view.rerender(
        launcher(
          Object.freeze({
            ...initialNleSurfaceState,
            status: "compact_unsupported" as const,
            lastCloseReason: "capability_or_mount_failure" as const,
          }),
          false,
        ),
      );
    });
    const after = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-focus-key="nle-open-overlay"]',
    );
    expect(after).toBe(button);
    expect(after!.disabled).toBe(false);
    expect(document.activeElement).toBe(button);
    const fallback = view.container.querySelector(
      '[data-h3-nle-unavailable="overlay_v1"]',
    )!;
    expect(fallback).not.toBeNull();
    expect(button.getAttribute("aria-describedby")).toBe(
      fallback.querySelector("p")!.id,
    );
  });

  it("returns focus to the launcher on the transition even when focus had moved on", () => {
    const view = render(launcher(initialNleSurfaceState, true));
    const other = document.createElement("button");
    document.body.append(other);
    act(() => other.focus());
    act(() => {
      view.rerender(
        launcher(
          Object.freeze({
            ...initialNleSurfaceState,
            status: "compact_unsupported" as const,
            lastCloseReason: "capability_or_mount_failure" as const,
          }),
          false,
        ),
      );
    });
    const button = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-focus-key="nle-open-overlay"]',
    )!;
    expect(document.activeElement).toBe(button);
    // A later re-render in the same refused state does not steal focus again.
    act(() => other.focus());
    act(() => {
      view.rerender(
        launcher(
          Object.freeze({
            ...initialNleSurfaceState,
            status: "compact_unsupported" as const,
            lastCloseReason: "capability_or_mount_failure" as const,
          }),
          false,
        ),
      );
    });
    expect(document.activeElement).toBe(other);
    other.remove();
  });

  it("renders only the fallback region on a host that is unsupported before any gesture", () => {
    const view = render(
      launcher(
        Object.freeze({
          ...initialNleSurfaceState,
          status: "compact_unsupported" as const,
        }),
        false,
      ),
    );
    expect(
      view.container.querySelector('[data-h3-focus-key="nle-open-overlay"]'),
    ).toBeNull();
    // M25-44 (one NLE): the region carries the surface status alone; no compact editor remains.
    const region = view.container.querySelector(
      '[data-h3-nle-unavailable="overlay_v1"]',
    );
    expect(region).not.toBeNull();
    expect(
      region!.querySelector('[data-h3-nle-status="surface"]')?.textContent,
    ).toBe("The full editor is unavailable in this browser.");
    expect(view.container.querySelector("[data-h3-nle-fallback]")).toBeNull();
  });
});
