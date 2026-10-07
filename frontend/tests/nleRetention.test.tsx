// M25-21 section 14.4, Clip editor row: the overlay geometry and pane apply only on an explicit
// open (clamped to the current viewport), the timeline view and inspector drafts survive view
// release for the same authority, the monitor returns paused at the retained frame, and stale
// revision-bound drafts are dropped with a visible notice. Nothing here is replayed on remount.

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleInspectorTabs } from "../src/components/nle/NleInspectorTabs";
import {
  NleTimeline,
  type NleTimelineTransportChannel,
  type NleTimelineTransportSnapshot,
} from "../src/components/nle/NleTimeline";
import { NleWorkspace } from "../src/components/nle/NleWorkspace";
import { createNleWorkspaceSession } from "../src/lifecycle/nleWorkspaceSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import type { NleMonitorStatus } from "../src/runtime/nleWorkspaceRuntime";
import type {
  VisualCompositionSession,
  VisualCompositionStatus,
} from "../src/runtime/visualCompositionSession";
import {
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  type RuntimeCapabilityObservation,
} from "../src/runtime/mediaCapabilities";
import {
  clampOverlayBounds,
  overlayDefaultBounds,
} from "../src/runtime/nleOverlayGeometry";
import { createSidebarRetention } from "../src/state/sidebarRetention";
import { UNSUPPORTED_OUTPUT_CAPABILITY } from "../src/host/authoringOutputCapabilityClient";
import {
  SMOKE_SHAPE,
  authoringReady,
  snapshotFixture,
} from "./support/nleWorkspaceFixture";
import {
  availableDisposition,
  bindingFixture,
} from "./support/nleWorkspaceBinding";

const harness = vi.hoisted(() => ({
  create: null as
    null | ((canvas: HTMLCanvasElement, binding: unknown) => unknown),
  observation: null as unknown,
}));

vi.mock("../src/runtime/nleWorkspaceRuntime", async (importOriginal) => {
  const original =
    await importOriginal<typeof import("../src/runtime/nleWorkspaceRuntime")>();
  return {
    ...original,
    createNleMonitor: (canvas: HTMLCanvasElement, binding: unknown) =>
      harness.create!(canvas, binding),
  };
});

vi.mock("../src/runtime/mediaCapabilities", async (importOriginal) => {
  const original =
    await importOriginal<typeof import("../src/runtime/mediaCapabilities")>();
  return {
    ...original,
    observeBrowserMediaCapabilities: () =>
      (harness.observation as RuntimeCapabilityObservation | null) ??
      original.observeBrowserMediaCapabilities(),
  };
});

afterEach(() => {
  cleanup();
  harness.create = null;
  harness.observation = null;
  document.body.innerHTML = "";
});

function compositionStatus(
  status: VisualCompositionStatus["status"],
): VisualCompositionStatus {
  return Object.freeze({
    status,
    blocker: null,
    browserPreviewOnly: true,
    durationFrames: 2880,
  });
}

function monitorHarness() {
  const listeners = new Set<() => void>();
  let current: NleMonitorStatus = Object.freeze({
    composition: compositionStatus("opening"),
    audio: null,
    frame: null,
    presentedFrame: null,
  });
  const session = {
    open: vi.fn(async () => undefined),
    seek: vi.fn(async () => undefined),
    step: vi.fn(async () => undefined),
    play: vi.fn(async () => undefined),
    pause: vi.fn(async () => undefined),
    recover: vi.fn(async () => undefined),
  } as unknown as VisualCompositionSession;
  const dispose = vi.fn(async () => undefined);
  harness.create = () => {
    listeners.clear();
    current = Object.freeze({
      composition: compositionStatus("opening"),
      audio: null,
      frame: null,
      presentedFrame: null,
    });
    return {
      session,
      status: () => current,
      subscribe(listener: () => void) {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      replace: vi.fn(async () => undefined),
      dispose,
    };
  };
  return {
    session,
    dispose,
    openingTarget(call = -1) {
      const target = vi.mocked(session.open).mock.calls.at(call)?.[0];
      return typeof target === "function" ? target() : target;
    },
    publish(next: Partial<NleMonitorStatus>) {
      current = Object.freeze({ ...current, ...next });
      for (const listener of listeners) listener();
    },
  };
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

describe("M25-21 NLE overlay geometry retention", () => {
  function subject() {
    const session = createShellSession();
    const container = document.createElement("div");
    document.body.append(container);
    session.container = container;
    const runAuthoringIntent = vi.fn(async () => undefined);
    const runtime = {
      session,
      deps: {
        authoringOutputCapabilityClient: {
          read: vi.fn(async () => UNSUPPORTED_OUTPUT_CAPABILITY),
        },
      },
      actions: {
        renderCurrent: vi.fn(),
        runAuthoringIntent,
        mediaRuntimeLeaveContext: vi.fn(),
      },
    } as unknown as ShellRuntime;
    return { session, nle: createNleWorkspaceSession(runtime) };
  }

  function supported(): RuntimeCapabilityObservation {
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

  function setViewport(width: number, height: number): void {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: width,
    });
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      value: height,
    });
  }

  it("applies retained geometry and pane only on an explicit open, clamped to the viewport", () => {
    harness.observation = supported();
    setViewport(1600, 1000);
    const { session, nle } = subject();
    nle.nleOpenOverlay();
    nle.nleOverlayMounted(1);
    nle.nleSelectPane("sequence");
    nle.nleResizeOverlay({ width: 1100, height: 700 });
    nle.nleDisposeOverlay();
    // View destroy keeps the editor closed; the surface itself resets as before.
    expect(session.nleWorkspace.surface.status).toBe("compact_ready");
    expect(session.nleWorkspace.surface.bounds).toEqual({
      width: 0,
      height: 0,
    });
    expect(session.nleWorkspace.surface.pane).toBe("assets");

    nle.nleOpenOverlay();
    expect(session.nleWorkspace.surface.pane).toBe("sequence");
    expect(session.nleWorkspace.surface.bounds).toEqual({
      width: 1100,
      height: 700,
    });
    nle.nleOverlayMounted(2);
    nle.nleCloseOverlay("explicit_close");
    nle.nleOverlayReleased(2);

    // A smaller viewport clamps the retained request instead of overflowing it.
    setViewport(1000, 640);
    nle.nleOpenOverlay();
    const clamped = clampOverlayBounds(
      { width: 1000, height: 640 },
      { width: 1100, height: 700 },
    );
    expect(session.nleWorkspace.surface.bounds).toEqual(clamped);
    expect(clamped.width).toBeLessThan(1100);

    nle.nleOverlayMounted(3);
    nle.nleCloseOverlay("explicit_close");
    nle.nleOverlayReleased(3);
    session.retention.dispose();
    nle.nleOpenOverlay();
    expect(session.nleWorkspace.surface.pane).toBe("assets");
    expect(session.nleWorkspace.surface.bounds).toEqual(
      overlayDefaultBounds({ width: 1000, height: 640 }),
    );
  });
});

describe("M25-21 NLE timeline view retention", () => {
  function timeline(
    retention: ReturnType<typeof createSidebarRetention>,
    snapshot = snapshotFixture(SMOKE_SHAPE),
  ) {
    return (
      <NleTimeline
        locale="en"
        snapshot={snapshot}
        selection={[]}
        authoring={authoringReady(SMOKE_SHAPE)}
        gridFrames={1}
        playheadFrame={null}
        highlightedAssetIds={[]}
        onIntent={vi.fn(async () => undefined)}
        onEdgeGestureActive={() => undefined}
        retention={retention}
      />
    );
  }
  const control = (id: string) =>
    document.querySelector<HTMLInputElement>(`[data-h3-nle-control="${id}"]`)!;

  it("restores zoom, snap and view start for the same workspace", () => {
    const retention = createSidebarRetention();
    const first = render(timeline(retention));
    fireEvent.click(control("transport.zoom_in"));
    fireEvent.click(control("transport.zoom_in"));
    fireEvent.click(control("transport.snap"));
    fireEvent.change(control("transport.scroll"), { target: { value: "480" } });
    const zoomed = screen.getByText("Zoom 4x");
    expect(zoomed).toBeDefined();
    first.unmount();

    render(timeline(retention));
    expect(screen.getByText("Zoom 4x")).toBeDefined();
    expect(control("transport.snap").getAttribute("aria-pressed")).toBe("true");
    expect(control("transport.scroll").value).toBe("480");
  });

  it("steps the view start itself when a page-wide hotkey cancels its native default", () => {
    // B-M2605-SEEK-02: ComfyUI-Easy-Use binds the arrows through hotkeys-js on `document`, which
    // cancels the native step of every range input, so the scroll range lost its arrow keys.
    const foreign = vi.fn((event: KeyboardEvent) => event.preventDefault());
    document.addEventListener("keydown", foreign);
    try {
      const view = render(timeline(createSidebarRetention()));
      fireEvent.click(control("transport.zoom_in"));
      fireEvent.click(control("transport.zoom_in"));
      const scroll = control("transport.scroll");
      const max = Number(scroll.max);
      const step = Number(scroll.step);
      const initial = Number(scroll.value);
      expect(max).toBeGreaterThan(step);
      const press = (key: string, init: KeyboardEventInit = {}) =>
        fireEvent.keyDown(scroll, { key, ...init });
      expect(press("ArrowRight")).toBe(false);
      expect(Number(scroll.value)).toBe(initial + step);
      press("ArrowLeft");
      expect(Number(scroll.value)).toBe(initial);
      press("End");
      expect(Number(scroll.value)).toBe(Math.floor(max / step) * step);
      press("Home");
      expect(Number(scroll.value)).toBe(0);
      expect(foreign).not.toHaveBeenCalled();
      // Browser and OS shortcuts keep their meaning.
      press("ArrowRight", { altKey: true });
      expect(Number(scroll.value)).toBe(0);
      expect(foreign).toHaveBeenCalledTimes(1);
      view.unmount();
    } finally {
      document.removeEventListener("keydown", foreign);
    }
  });

  it("does not settle a new same-target ruler request from an older monitor request", async () => {
    const oldSeek = deferred<void>();
    const newSeek = deferred<void>();
    let state: NleTimelineTransportSnapshot = {
      frame: 0,
      request: { generation: 1, frame: 2879 } as const,
      settledRequestGeneration: 0,
      transportAvailable: true,
    };
    const listeners = new Set<() => void>();
    const channel: NleTimelineTransportChannel = {
      snapshot: () => state,
      subscribe: (listener) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    };
    const publish = (next: typeof state) => {
      state = next;
      for (const listener of listeners) listener();
    };
    const trace: Array<Record<string, unknown>> = [];
    const onSeek = vi.fn(() => newSeek.promise);
    const view = render(
      <NleTimeline
        locale="en"
        snapshot={snapshotFixture(SMOKE_SHAPE)}
        selection={[]}
        authoring={authoringReady(SMOKE_SHAPE)}
        gridFrames={1}
        playheadFrame={0}
        transportAvailable
        transportChannel={channel}
        highlightedAssetIds={[]}
        onIntent={vi.fn(async () => undefined)}
        onEdgeGestureActive={() => undefined}
        onSeek={onSeek}
        retention={createSidebarRetention()}
      />,
    );
    const seek = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-control="transport.seek"]',
    )!;
    const record = (
      step: string,
      latestTarget: number,
      pending: string,
      presented: number,
    ) =>
      trace.push({
        step,
        latest_user_target: latestTarget,
        monitor_pending_request: pending,
        presented_frame: presented,
        ruler_value: Number(seek.getAttribute("aria-valuenow")),
        clear_reason: seek.dataset.h3NleRequestClearReason ?? null,
        acknowledged_generation: seek.dataset.h3NleRequestGeneration ?? null,
      });

    record("old_request_active", 2879, "generation-1@2879", 0);
    fireEvent.keyDown(seek, { key: "End" });
    expect(onSeek).toHaveBeenCalledExactlyOnceWith(2879);
    record("new_same_target_queued", 2879, "generation-1@2879", 0);
    act(() =>
      publish({
        frame: 1,
        request: { generation: 1, frame: 2879 },
        settledRequestGeneration: 0,
        transportAvailable: true,
      }),
    );
    record("old_request_published", 2879, "generation-1@2879", 1);
    oldSeek.resolve();
    act(() =>
      publish({
        frame: 1,
        request: null,
        settledRequestGeneration: 1,
        transportAvailable: true,
      }),
    );
    record("old_request_settled", 2879, "new request still pending", 1);

    expect(seek.getAttribute("aria-valuenow"), JSON.stringify(trace)).toBe(
      "2879",
    );
    expect(trace).toEqual([
      {
        step: "old_request_active",
        latest_user_target: 2879,
        monitor_pending_request: "generation-1@2879",
        presented_frame: 0,
        ruler_value: 0,
        clear_reason: "never_requested",
        acknowledged_generation: "unacknowledged",
      },
      {
        step: "new_same_target_queued",
        latest_user_target: 2879,
        monitor_pending_request: "generation-1@2879",
        presented_frame: 0,
        ruler_value: 2879,
        clear_reason: "pending_monitor_acknowledgement",
        acknowledged_generation: "unacknowledged",
      },
      {
        step: "old_request_published",
        latest_user_target: 2879,
        monitor_pending_request: "generation-1@2879",
        presented_frame: 1,
        ruler_value: 2879,
        clear_reason: "pending_monitor_acknowledgement",
        acknowledged_generation: "unacknowledged",
      },
      {
        step: "old_request_settled",
        latest_user_target: 2879,
        monitor_pending_request: "new request still pending",
        presented_frame: 1,
        ruler_value: 2879,
        clear_reason: "pending_monitor_acknowledgement",
        acknowledged_generation: "unacknowledged",
      },
    ]);
  });
});

describe("M25-50 tab-scoped inspector retention", () => {
  const selection = ["clip-3"];
  const NOTICE =
    "An unsent edit in this section was cleared because the timeline changed.";
  const OTHER_WORKSPACE = "authoring-" + "b".repeat(32);

  function inspector(
    retention: ReturnType<typeof createSidebarRetention>,
    onIntent = vi.fn(async () => undefined),
    revision = 11,
    options: { selection?: string[]; workspaceHandle?: string } = {},
  ) {
    const chosen = options.selection ?? selection;
    const snapshot = snapshotFixture(SMOKE_SHAPE, revision);
    return (
      <NleInspectorTabs
        locale="en"
        snapshot={
          options.workspaceHandle === undefined
            ? snapshot
            : { ...snapshot, workspaceHandle: options.workspaceHandle }
        }
        selection={chosen}
        authoring={authoringReady(SMOKE_SHAPE, { selection: chosen, revision })}
        onIntent={onIntent}
        retention={retention}
      />
    );
  }
  const openTab = (name: string) =>
    fireEvent.click(screen.getByRole("tab", { name }));
  const textDraft = () =>
    screen.getByRole("textbox", { name: "Text" }) as HTMLTextAreaElement;
  const opacityDraft = () =>
    screen.getByRole("spinbutton", {
      name: "Opacity (%)",
    }) as HTMLInputElement;

  function editDistinctiveDrafts(): void {
    fireEvent.change(opacityDraft(), { target: { value: "43.21" } });
    openTab("Text");
    fireEvent.change(textDraft(), {
      target: { value: "M25-50 unsent title Ω" },
    });
  }

  it("restores unsent drafts for the same workspace and revision without dispatching", () => {
    const retention = createSidebarRetention();
    const onIntent = vi.fn(async () => undefined);
    const first = render(inspector(retention, onIntent));
    editDistinctiveDrafts();
    first.unmount();

    render(inspector(retention, onIntent));
    expect(
      screen.getByRole("tab", { name: "Text" }).getAttribute("aria-selected"),
    ).toBe("true");
    expect(textDraft().value).toBe("M25-50 unsent title Ω");
    openTab("Basic");
    expect(opacityDraft().value).toBe("43.21");
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("never reports an untouched form as a lost draft", () => {
    const retention = createSidebarRetention();
    const first = render(inspector(retention));
    first.unmount();
    // The selected tab is view state; it is not reported as a lost draft.
    expect(retention.size()).toBe(1);

    const later = render(inspector(retention, undefined, 12));
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
    later.unmount();
    render(
      inspector(retention, undefined, 12, { workspaceHandle: OTHER_WORKSPACE }),
    );
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
  });

  it("does not carry tab drafts or a hidden Text tab to another clip", () => {
    const retention = createSidebarRetention();
    const view = render(inspector(retention));
    openTab("Text");
    fireEvent.change(textDraft(), {
      target: { value: "M25-50 unsent title Ω" },
    });
    view.rerender(
      inspector(retention, undefined, 11, { selection: ["clip-0"] }),
    );
    expect(
      screen.getByRole("tab", { name: "Basic" }).getAttribute("aria-selected"),
    ).toBe("true");
    expect(screen.queryByRole("tab", { name: "Text" })).toBeNull();
    expect(screen.queryAllByText(NOTICE)).toHaveLength(1);
    view.rerender(
      inspector(retention, undefined, 11, { selection: ["clip-3"] }),
    );
    openTab("Text");
    expect(textDraft().value).toBe("Title clip-3");
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
  });

  it("reports a property draft lost to a changed workspace once, until the next edit", () => {
    const retention = createSidebarRetention();
    const first = render(inspector(retention));
    fireEvent.change(opacityDraft(), { target: { value: "43.21" } });
    first.unmount();

    const replaced = render(
      inspector(retention, undefined, 11, { workspaceHandle: OTHER_WORKSPACE }),
    );
    expect(opacityDraft().value).not.toBe("43.21");
    expect(screen.getByText(NOTICE).getAttribute("role")).toBe("status");
    expect(screen.getAllByText(NOTICE)).toHaveLength(1);
    fireEvent.change(opacityDraft(), { target: { value: "56.78" } });
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
    replaced.unmount();

    render(
      inspector(retention, undefined, 11, { workspaceHandle: OTHER_WORKSPACE }),
    );
    expect(opacityDraft().value).toBe("56.78");
    expect(screen.queryAllByText(NOTICE)).toHaveLength(0);
  });

  it("drops every tab draft after an accepted revision and reports the loss once", () => {
    const retention = createSidebarRetention();
    const first = render(inspector(retention));
    editDistinctiveDrafts();
    first.unmount();

    render(
      inspector(
        retention,
        vi.fn(async () => undefined),
        12,
      ),
    );
    expect(
      screen.getByRole("tab", { name: "Text" }).getAttribute("aria-selected"),
    ).toBe("true");
    expect(textDraft().value).toBe("Title clip-3");
    expect(screen.getByText(NOTICE).getAttribute("role")).toBe("status");
    expect(screen.getAllByText(NOTICE)).toHaveLength(1);
    openTab("Basic");
    expect(opacityDraft().value).not.toBe("43.21");
  });
});

describe("M25-21 NLE paused playhead restoration", () => {
  function workspace(retention: ReturnType<typeof createSidebarRetention>) {
    const binding = bindingFixture({
      runtime: availableDisposition(),
      authoring: authoringReady(SMOKE_SHAPE),
      retention,
    });
    return (
      <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />
    );
  }

  it("returns paused at the retained frame and never resumes playback", () => {
    const monitor = monitorHarness();
    const retention = createSidebarRetention();
    const first = render(workspace(retention));
    act(() => {
      monitor.publish({
        composition: compositionStatus("playing"),
        frame: 240,
      });
    });
    first.unmount();
    // View release disposes the owned monitor; the frame survives it, the playback does not.
    expect(monitor.dispose).toHaveBeenCalledTimes(1);
    expect(monitor.session.seek).not.toHaveBeenCalled();

    const second = render(workspace(retention));
    expect(monitor.session.open).toHaveBeenCalledTimes(2);
    expect(monitor.openingTarget()).toBe(240);
    expect(monitor.session.seek).not.toHaveBeenCalled();
    expect(monitor.session.play).not.toHaveBeenCalled();
    // Released while the restore is still in flight: the presented opening frame is not the
    // user's position, so the retained frame stands for the next open.
    act(() => {
      monitor.publish({ composition: compositionStatus("paused"), frame: 0 });
    });
    second.unmount();
    render(workspace(retention));
    expect(monitor.openingTarget()).toBe(240);
    expect(monitor.session.seek).not.toHaveBeenCalled();
    expect(monitor.session.play).not.toHaveBeenCalled();
  });

  it("retains the position the user asked for before the media presents it", async () => {
    // M25-21 B3-D33: a seek the user has made but the media has not presented yet is still their
    // position -- the transport shows it (B3-D10) -- so releasing the view immediately after a
    // seek must not retain the frame the seek moved away from.
    const monitor = monitorHarness();
    const retention = createSidebarRetention();
    const first = render(workspace(retention));
    act(() => {
      monitor.publish({ composition: compositionStatus("paused"), frame: 0 });
    });
    // The seek never settles: the media is still fetching the requested position at release.
    vi.mocked(monitor.session.seek).mockImplementationOnce(
      () => new Promise<void>(() => undefined),
    );
    const seek = first.container.querySelector<HTMLElement>(
      '[data-h3-nle-control="transport.seek"]',
    )!;
    fireEvent.keyDown(seek, { key: "PageUp" });
    expect(seek.getAttribute("aria-valuenow")).toBe("24");
    await waitFor(() =>
      expect(monitor.session.seek).toHaveBeenCalledExactlyOnceWith(24),
    );
    first.unmount();

    render(workspace(retention));
    expect(monitor.session.seek).toHaveBeenCalledExactlyOnceWith(24);
    expect(monitor.openingTarget()).toBe(24);
    expect(monitor.session.play).not.toHaveBeenCalled();
  });

  it("accumulates rapid ruler page steps from the latest requested frame", async () => {
    const monitor = monitorHarness();
    const retention = createSidebarRetention();
    const view = render(workspace(retention));
    act(() => {
      monitor.publish({ composition: compositionStatus("paused"), frame: 0 });
    });
    const seek = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-control="transport.seek"]',
    )!;

    act(() => {
      seek.dispatchEvent(
        new KeyboardEvent("keydown", { key: "PageUp", bubbles: true }),
      );
      seek.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Home", bubbles: true }),
      );
      seek.dispatchEvent(
        new KeyboardEvent("keydown", { key: "PageUp", bubbles: true }),
      );
      seek.dispatchEvent(
        new KeyboardEvent("keydown", { key: "PageUp", bubbles: true }),
      );
      seek.dispatchEvent(
        new KeyboardEvent("keydown", { key: "PageUp", bubbles: true }),
      );
    });

    expect(seek.getAttribute("aria-valuenow")).toBe("72");
    await waitFor(() =>
      expect(monitor.session.seek).toHaveBeenLastCalledWith(72),
    );
  });

  it("keeps Home authoritative when the older seek publishes between requests", async () => {
    const monitor = monitorHarness();
    const firstSeek = deferred();
    const homeSeek = deferred();
    const laterSeeks = [deferred(), deferred(), deferred()];
    vi.mocked(monitor.session.seek)
      .mockImplementationOnce(() => firstSeek.promise)
      .mockImplementationOnce(() => homeSeek.promise)
      .mockImplementationOnce(() => laterSeeks[0].promise)
      .mockImplementationOnce(() => laterSeeks[1].promise)
      .mockImplementationOnce(() => laterSeeks[2].promise);
    const view = render(workspace(createSidebarRetention()));
    act(() => {
      monitor.publish({ composition: compositionStatus("paused"), frame: 0 });
    });
    const seek = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-control="transport.seek"]',
    )!;
    const trace: Array<Record<string, unknown>> = [];
    const record = (step: string, pending: string) =>
      trace.push({
        step,
        latest_user_target: Number(seek.getAttribute("aria-valuenow")),
        monitor_pending_request: pending,
        presented_frame: Number(
          view.container
            .querySelector('[data-h3-nle-timecode-part="current"]')!
            .textContent!.replaceAll(":", ""),
        ),
        clear_reason: seek.dataset.h3NleRequestClearReason ?? null,
      });

    fireEvent.keyDown(seek, { key: "PageUp" });
    await waitFor(() =>
      expect(monitor.session.seek).toHaveBeenLastCalledWith(24),
    );
    record("old_seek_pending", "seek-1@24");
    fireEvent.keyDown(seek, { key: "Home" });
    await waitFor(() =>
      expect(monitor.session.seek).toHaveBeenLastCalledWith(0),
    );
    record("home_pending", "seek-2@0");
    act(() => monitor.publish({ frame: 24 }));
    firstSeek.resolve();
    await act(async () => Promise.resolve());
    record("old_seek_published_and_settled", "seek-2@0");
    act(() => {
      seek.dispatchEvent(
        new KeyboardEvent("keydown", { key: "PageUp", bubbles: true }),
      );
      seek.dispatchEvent(
        new KeyboardEvent("keydown", { key: "PageUp", bubbles: true }),
      );
      seek.dispatchEvent(
        new KeyboardEvent("keydown", { key: "PageUp", bubbles: true }),
      );
    });
    record("three_page_steps", "latest@72");

    expect(seek.getAttribute("aria-valuenow"), JSON.stringify(trace)).toBe(
      "72",
    );
  });

  it("does not acknowledge a new same-target request from the older request", async () => {
    const monitor = monitorHarness();
    const oldSeek = deferred();
    const pauseGate = deferred();
    const newSeek = deferred();
    vi.mocked(monitor.session.seek)
      .mockImplementationOnce(() => oldSeek.promise)
      .mockImplementationOnce(() => newSeek.promise);
    vi.mocked(monitor.session.pause).mockImplementationOnce(
      () => pauseGate.promise,
    );
    const view = render(workspace(createSidebarRetention()));
    act(() => {
      monitor.publish({ composition: compositionStatus("paused"), frame: 0 });
    });
    const seek = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-control="transport.seek"]',
    )!;
    const trace: Array<Record<string, unknown>> = [];
    const record = (step: string, pending: string, presented: number) =>
      trace.push({
        step,
        latest_user_target: Number(seek.getAttribute("aria-valuenow")),
        monitor_pending_request: pending,
        presented_frame: presented,
        clear_reason: seek.dataset.h3NleRequestClearReason ?? null,
      });

    fireEvent.keyDown(seek, { key: "End" });
    await waitFor(() =>
      expect(monitor.session.seek).toHaveBeenCalledExactlyOnceWith(2879),
    );
    record("old_same_target_pending", "seek-1@2879", 0);
    act(() => {
      monitor.publish({ composition: compositionStatus("playing"), frame: 0 });
    });
    fireEvent.keyDown(seek, { key: "End" });
    expect(monitor.session.pause).toHaveBeenCalledTimes(1);
    expect(monitor.session.seek).toHaveBeenCalledTimes(1);
    record("new_same_target_waits_for_pause", "seek-1@2879", 0);
    act(() => monitor.publish({ frame: 1 }));
    record("old_request_publishes", "seek-1@2879", 1);
    oldSeek.resolve();
    await act(async () => Promise.resolve());
    record("old_request_settles", "new@2879 waiting for pause", 1);

    expect(seek.getAttribute("aria-valuenow"), JSON.stringify(trace)).toBe(
      "2879",
    );
    pauseGate.resolve();
    await waitFor(() => expect(monitor.session.seek).toHaveBeenCalledTimes(2));
  });
});
