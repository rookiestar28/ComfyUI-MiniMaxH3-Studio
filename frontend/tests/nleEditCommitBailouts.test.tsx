// An accepted edit re-renders the editor twice -- pending, then ready -- and the decoded C3 budget
// (16 ms p95 per commit) is spent on every view that renders in them. The views below have nothing
// to change in an edit and must bail out of it. Each case also proves the view still follows a
// change it does show, and that the stable handlers it receives run the newest committed behaviour.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import {
  StrictMode,
  Suspense,
  createElement,
  startTransition,
  useState,
  type ComponentProps,
} from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const probe = vi.hoisted(() => ({
  transportButtons: 0,
  rulerLabels: 0,
  copyReads: 0,
  sortMenus: 0,
  create: null as
    null | ((canvas: HTMLCanvasElement, binding: unknown) => unknown),
}));

// Counts a render of the transport row: it renders these five actions and nothing else does.
vi.mock("../src/components/nle/NleIconActions", async (importOriginal) => {
  const actual =
    await importOriginal<
      typeof import("../src/components/nle/NleIconActions")
    >();
  return {
    ...actual,
    NleIconButton: (props: ComponentProps<typeof actual.NleIconButton>) => {
      if (props.control.startsWith("transport.")) probe.transportButtons++;
      return createElement(actual.NleIconButton, props);
    },
  };
});

// Counts a render of the ruler: only its marks are labelled with this formatter.
vi.mock("../src/runtime/timelineGeometry", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../src/runtime/timelineGeometry")>();
  return {
    ...actual,
    formatRulerLabel: (...args: Parameters<typeof actual.formatRulerLabel>) => {
      probe.rulerLabels++;
      return actual.formatRulerLabel(...args);
    },
  };
});

// Counts renders of a lone status-chip pair: each render reads its copy table once per view.
vi.mock("../src/components/nle/nleCopy", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../src/components/nle/nleCopy")>();
  return {
    ...actual,
    nleCopy: (...args: Parameters<typeof actual.nleCopy>) => {
      probe.copyReads++;
      return actual.nleCopy(...args);
    },
  };
});

// Counts renders of the media bin's toolbar row: it is the only owner of the sort menu.
vi.mock("../src/components/nle/NleBinMenus", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../src/components/nle/NleBinMenus")>();
  return {
    ...actual,
    NleMediaSortFilterMenu: (
      props: ComponentProps<typeof actual.NleMediaSortFilterMenu>,
    ) => {
      probe.sortMenus++;
      return createElement(actual.NleMediaSortFilterMenu, props);
    },
  };
});

vi.mock("../src/runtime/nleWorkspaceRuntime", async (importOriginal) => {
  const original =
    await importOriginal<typeof import("../src/runtime/nleWorkspaceRuntime")>();
  return {
    ...original,
    createNleMonitor: (canvas: HTMLCanvasElement, binding: unknown) =>
      probe.create!(canvas, binding),
  };
});

import { NleMediaBin } from "../src/components/nle/NleMediaBin";
import { NleMonitor } from "../src/components/nle/NleMonitor";
import { createNleBinInsertChannel } from "../src/runtime/nleBinInsertChannel";
import { NleMonitorChips } from "../src/components/nle/NleMonitorChips";
import { createMonitorStatusChannel } from "../src/components/nle/nleMonitorChannel";
import {
  NleTimeline,
  type NleTimelineProps,
} from "../src/components/nle/NleTimeline";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import type { NleMonitorStatus } from "../src/runtime/nleWorkspaceRuntime";
import type {
  VisualCompositionSession,
  VisualCompositionStatus,
} from "../src/runtime/visualCompositionSession";
import {
  SMOKE_SHAPE,
  authoringReady,
  snapshotFixture,
} from "./support/nleWorkspaceFixture";
import { availableDisposition } from "./support/nleWorkspaceBinding";

const {
  deriveNleMonitorBinding,
}: typeof import("../src/runtime/nleWorkspaceRuntime") = await vi.importActual(
  "../src/runtime/nleWorkspaceRuntime",
);

afterEach(() => {
  cleanup();
  probe.create = null;
  probe.transportButtons = 0;
  probe.rulerLabels = 0;
  probe.copyReads = 0;
  probe.sortMenus = 0;
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
    resize: vi.fn(() => undefined),
    seek: vi.fn(async () => undefined),
    step: vi.fn(async () => undefined),
    play: vi.fn(async () => undefined),
    pause: vi.fn(async () => undefined),
    recover: vi.fn(async () => undefined),
    replace: vi.fn(async () => undefined),
    close: vi.fn(async () => undefined),
  } as unknown as VisualCompositionSession;
  probe.create = () => ({
    session,
    status: () => current,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    replace: vi.fn(async () => undefined),
    dispose: vi.fn(async () => undefined),
  });
  return {
    session,
    publish(next: Partial<NleMonitorStatus>) {
      current = Object.freeze({ ...current, ...next });
      act(() => {
        for (const listener of listeners) listener();
      });
    },
  };
}

type TimelineBridge = Readonly<{ seek(frame: number): void }>;

function monitorProps(bridge?: { current: TimelineBridge | null }) {
  return {
    locale: "en" as const,
    monitor: deriveNleMonitorBinding(
      snapshotFixture(SMOKE_SHAPE),
      availableDisposition(),
      {} as AuthoringMediaSourceLeaseClient,
    ),
    selectedClip: undefined,
    onFrame: vi.fn(),
    bindTimelineTransport:
      bridge === undefined
        ? undefined
        : (next: TimelineBridge | null) => {
            bridge.current = next;
            return () => {
              if (bridge.current === next) bridge.current = null;
            };
          },
  };
}

describe("monitor transport row", () => {
  it("bails out of the pending and ready renders of an edit", () => {
    const monitor = monitorHarness();
    const props = monitorProps();
    const view = render(<NleMonitor {...props} />);
    monitor.publish({ composition: compositionStatus("paused"), frame: 0 });
    const settled = probe.transportButtons;
    expect(settled).toBeGreaterThan(0);

    // What an accepted edit changes on the monitor: the transform authority and its handlers.
    view.rerender(
      <NleMonitor
        {...props}
        onFrame={vi.fn()}
        transformEditable={false}
        onTransformCommit={vi.fn()}
      />,
    );
    view.rerender(
      <NleMonitor
        {...props}
        onFrame={vi.fn()}
        transformEditable
        onTransformCommit={vi.fn()}
      />,
    );
    expect(probe.transportButtons).toBe(settled);

    // A change the row shows still renders it.
    monitor.publish({ composition: compositionStatus("playing") });
    expect(probe.transportButtons).toBeGreaterThan(settled);
    expect(
      view.container.querySelector('[data-h3-nle-control="transport.pause"]'),
    ).not.toBeNull();
  });

  it("steps from the newest requested frame through its stable handler", () => {
    const monitor = monitorHarness();
    const bridge: { current: TimelineBridge | null } = { current: null };
    const view = render(<NleMonitor {...monitorProps(bridge)} />);
    monitor.publish({ composition: compositionStatus("paused"), frame: 0 });
    vi.mocked(monitor.session.seek).mockImplementation(
      () => new Promise<void>(() => undefined),
    );
    // The ruler's seek is still in flight: the monitor holds it as the requested frame, and the
    // row re-renders to show it while its handler identities stay the same.
    act(() => bridge.current!.seek(24));
    fireEvent.click(
      view.container.querySelector(
        '[data-h3-nle-control="transport.step_forward"]',
      )!,
    );
    // B-M2544-05: a step while a seek is unpresented steps from that seek. A handler captured
    // before the request would see no requested frame and step the session from the old one.
    expect(monitor.session.seek).toHaveBeenLastCalledWith(25);
    expect(monitor.session.step).not.toHaveBeenCalled();
  });
});

describe("monitor status chips", () => {
  it("bail out of overlay renders and still follow their channel", () => {
    const channel = createMonitorStatusChannel();
    const Overlay = ({ edit }: { edit: number }) => (
      <div data-edit={edit}>
        <NleMonitorChips locale="en" channel={channel} />
      </div>
    );
    const view = render(<Overlay edit={0} />);
    const settled = probe.copyReads;
    expect(settled).toBeGreaterThan(0);
    view.rerender(<Overlay edit={1} />);
    view.rerender(<Overlay edit={2} />);
    expect(probe.copyReads).toBe(settled);

    act(() =>
      channel.publish(
        Object.freeze({
          composition: compositionStatus("paused"),
          audio: null,
          frame: 0,
          presentedFrame: 0,
        }),
      ),
    );
    expect(probe.copyReads).toBeGreaterThan(settled);
    expect(
      view.container.querySelector('[data-h3-nle-status="monitor"]')!
        .textContent,
    ).not.toBe("");
  });
});

function timelineProps(): NleTimelineProps {
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

describe("media bin toolbar row", () => {
  it("bails out of an edit and still follows its own controls", () => {
    const channel = createNleBinInsertChannel();
    const leaseClient = {} as AuthoringMediaSourceLeaseClient;
    const bin = (busy: boolean, snapshot = snapshotFixture(SMOKE_SHAPE)) => (
      <NleMediaBin
        locale="en"
        snapshot={snapshot}
        highlightedAssetIds={[]}
        busy={busy}
        runtimeEpoch={1}
        leaseClient={leaseClient}
        currentFrame={() => 0}
        binInsert={channel}
        onIntent={vi.fn(async () => undefined)}
      />
    );
    const view = render(bin(false));
    const settled = probe.sortMenus;
    // jsdom has no element scrolling; the bin scrolls its list back to the top on each control.
    view.container.querySelector<HTMLElement>(
      ".h3-nle-media-scroll",
    )!.scrollTo = vi.fn();
    expect(settled).toBeGreaterThan(0);
    // Pending, then ready with the next accepted snapshot and new handlers.
    view.rerender(bin(true));
    view.rerender(bin(false, snapshotFixture(SMOKE_SHAPE, 12)));
    expect(probe.sortMenus).toBe(settled);
    expect(
      view.container.querySelector<HTMLButtonElement>(".h3-nle-media-primary")!
        .disabled,
    ).toBe(false);

    fireEvent.change(
      view.container.querySelector<HTMLInputElement>(
        '[data-h3-nle-control="media.search"]',
      )!,
      { target: { value: "zz-no-match" } },
    );
    expect(probe.sortMenus).toBeGreaterThan(settled);
    fireEvent.click(
      view.container.querySelector('[data-h3-nle-control="media.view.list"]')!,
    );
    expect(
      view.container
        .querySelector("[data-h3-nle-media-view]")!
        .getAttribute("data-h3-nle-media-view"),
    ).toBe("list");
  });
});

// A render that suspends inside a transition is abandoned until released; nothing it rendered may
// reach a handler before it commits (parent BR4).
function suspendedGate() {
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
  return { Gate, release: () => release() };
}

describe("timeline ruler", () => {
  it("bails out of the pending and ready renders of an edit", () => {
    const input = timelineProps();
    const view = render(<NleTimeline {...input} />);
    const settled = probe.rulerLabels;
    expect(settled).toBeGreaterThan(0);

    view.rerender(
      <NleTimeline
        {...input}
        authoring={
          {
            ...input.authoring,
            status: "pending",
          } as NleTimelineProps["authoring"]
        }
        onIntent={vi.fn(async () => undefined)}
      />,
    );
    view.rerender(
      <NleTimeline {...input} onIntent={vi.fn(async () => undefined)} />,
    );
    expect(probe.rulerLabels).toBe(settled);

    // A frame the ruler shows still renders it.
    view.rerender(<NleTimeline {...input} playheadFrame={48} />);
    expect(probe.rulerLabels).toBeGreaterThan(settled);
    expect(
      view.container
        .querySelector('[data-h3-nle-control="transport.seek"]')!
        .getAttribute("aria-valuenow"),
    ).toBe("48");
  });

  it("seeks through the newest committed seek owner", () => {
    const first = vi.fn();
    const second = vi.fn();
    const input = timelineProps();
    const view = render(<NleTimeline {...input} onSeek={first} />);
    view.rerender(<NleTimeline {...input} onSeek={second} />);
    const ruler = view.container.querySelector<HTMLElement>(
      '[data-h3-nle-control="transport.seek"]',
    )!;
    fireEvent.keyDown(ruler, { key: "End" });
    expect(second).toHaveBeenCalledExactlyOnceWith(
      Number(ruler.getAttribute("aria-valuemax")),
    );
    expect(first).not.toHaveBeenCalled();
  });

  it("never seeks through an abandoned render's owner", async () => {
    const first = vi.fn();
    const second = vi.fn();
    const input = timelineProps();
    const { Gate, release } = suspendedGate();
    let choose = (_next: typeof first) => {};
    function Harness() {
      const [onSeek, setOnSeek] = useState(() => first);
      choose = (next) => setOnSeek(() => next);
      return (
        <Suspense fallback={<p>Waiting</p>}>
          <NleTimeline {...input} onSeek={onSeek} />
          <Gate hold={onSeek === second} />
        </Suspense>
      );
    }
    const view = render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    );
    act(() => {
      startTransition(() => choose(second));
    });
    const ruler = () =>
      view.container.querySelector<HTMLElement>(
        '[data-h3-nle-control="transport.seek"]',
      )!;
    fireEvent.keyDown(ruler(), { key: "End" });
    expect(first).toHaveBeenCalledOnce();
    expect(second).not.toHaveBeenCalled();
    await act(async () => {
      release();
    });
    fireEvent.keyDown(ruler(), { key: "Home" });
    expect(second).toHaveBeenCalledExactlyOnceWith(0);
    expect(first).toHaveBeenCalledOnce();
  });
});
