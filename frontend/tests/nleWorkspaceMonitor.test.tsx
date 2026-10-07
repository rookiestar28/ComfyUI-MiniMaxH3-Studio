// M25-16 monitor: the binding derivation degrades by the accepted runtime disposition, the
// composition monitor delegates every transport control to the owned session (no UA media
// control), the embedded-audio state is read-only, the labeled selected-source fallback shows
// no compositor, and unmount disposes the monitor exactly once.

import { Profiler } from "react";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleMonitor } from "../src/components/nle/NleMonitor";
import {
  NleMonitorChips,
  monitorStatusText,
  monitorUnavailableReason,
} from "../src/components/nle/NleMonitorChips";
import { nleCopy } from "../src/components/nle/nleCopy";
import { createMonitorStatusChannel } from "../src/components/nle/nleMonitorChannel";
import { NleWorkspace } from "../src/components/nle/NleWorkspace";
import type { TimelineReceipt } from "../src/contracts/authoringWorkbenchCodec";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import type { NleMonitorStatus } from "../src/runtime/nleWorkspaceRuntime";
import type {
  VisualCompositionSession,
  VisualCompositionStatus,
} from "../src/runtime/visualCompositionSession";
import { SMOKE_SHAPE, snapshotFixture } from "./support/nleWorkspaceFixture";
import { projectionFixture } from "./support/authoringFixture";
import {
  availableDisposition,
  bindingFixture,
  unavailableDisposition,
} from "./support/nleWorkspaceBinding";

const harness = vi.hoisted(() => ({
  create: null as
    null | ((canvas: HTMLCanvasElement, binding: unknown) => unknown),
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

const {
  deriveNleMonitorBinding,
  sameMonitorBinding,
}: typeof import("../src/runtime/nleWorkspaceRuntime") = await vi.importActual(
  "../src/runtime/nleWorkspaceRuntime",
);

const leaseClient = {} as AuthoringMediaSourceLeaseClient;
type TimelineBridge = Readonly<{
  seek(frame: number): void;
  pause(): Promise<void>;
  available: boolean;
}>;

function timelineBridgeHarness() {
  let current: TimelineBridge | null = null;
  return {
    bind: (next: TimelineBridge | null) => {
      current = next;
      return () => {
        if (current === next) current = null;
      };
    },
    seek(frame: number) {
      if (current === null) throw new Error("timeline bridge unavailable");
      act(() => current!.seek(frame));
    },
    async pause() {
      if (current === null) throw new Error("timeline bridge unavailable");
      await act(() => current!.pause());
    },
  };
}

function currentTimecode(container: HTMLElement): string {
  return container.querySelector<HTMLElement>(
    '[data-h3-nle-timecode-part="current"]',
  )!.textContent!;
}

function compositionStatus(
  status: VisualCompositionStatus["status"],
  blocker: VisualCompositionStatus["blocker"] = null,
): VisualCompositionStatus {
  return Object.freeze({
    status,
    blocker,
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
    getVisualLayerGeometry: vi.fn(() => ({
      nativeCrop: { left: 0, top: 0, width: 320, height: 180 },
      sourceCrop: { left: 0, top: 0, width: 320, height: 180 },
      scaledWidth: 320,
      scaledHeight: 180,
      angleRadians: 0,
      centerX: 160,
      centerY: 90,
    })),
    previewVisualTransform: vi.fn(() => ({
      nativeCrop: { left: 0, top: 0, width: 320, height: 180 },
      sourceCrop: { left: 0, top: 0, width: 320, height: 180 },
      scaledWidth: 320,
      scaledHeight: 180,
      angleRadians: 0,
      centerX: 160,
      centerY: 90,
    })),
  } as unknown as VisualCompositionSession;
  const replace = vi.fn(async () => undefined);
  const dispose = vi.fn(async () => undefined);
  const created: unknown[] = [];
  harness.create = (canvas, binding) => {
    created.push({ canvas, binding });
    return {
      session,
      status: () => current,
      subscribe(listener: () => void) {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      replace,
      dispose,
    };
  };
  return {
    session,
    replace,
    dispose,
    created,
    publish(next: Partial<NleMonitorStatus>) {
      current = Object.freeze({ ...current, ...next });
      for (const listener of listeners) listener();
    },
  };
}

afterEach(() => {
  cleanup();
  harness.create = null;
});

describe("M25-16 monitor binding derivation", () => {
  it("keeps the monitor mounted on the accepted receipt while history refresh is pending", () => {
    const ctx = monitorHarness();
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    const next = snapshotFixture(SMOKE_SHAPE, 12);
    const projection = projectionFixture();
    const history = {
      schema: "h3.context.timeline_history_projection.v1" as const,
      workspaceHandle: snapshot.workspaceHandle,
      snapshot,
      selection: [],
      undoCursor: null,
      redoCursor: null,
      rejection: null,
    };
    const binding = bindingFixture({
      runtime: availableDisposition(),
      authoring: { status: "ready", projection, timelineHistory: history },
    });
    const rendered = render(
      <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />,
    );
    const receipt = {
      snapshot: next,
      selection: [],
      afterTimelineRevision: next.timelineRevision,
      affectedIds: [],
    } as unknown as TimelineReceipt;
    rendered.rerender(
      <NleWorkspace
        binding={{
          ...binding,
          authoring: {
            status: "pending",
            projection,
            lastTimelineReceipt: receipt,
          },
        }}
        onEdgeGestureActive={() => undefined}
      />,
    );
    expect(ctx.dispose).not.toHaveBeenCalled();
    expect(ctx.created).toHaveLength(1);
    expect(ctx.replace).toHaveBeenLastCalledWith(
      expect.objectContaining({ snapshot: next }),
      expect.any(Function),
    );
  });
  const snapshot = snapshotFixture(SMOKE_SHAPE);

  it("binds the composition monitor from an available disposition with a stable resolver", () => {
    const first = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const second = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    expect(first.mode).toBe("composition");
    expect(first.binding?.snapshot).toBe(snapshot);
    expect(first.binding?.manifest.manifestFingerprint).toMatch(/^sha256:/u);
    expect(first.binding?.leaseClient).toBe(leaseClient);
    expect(first.binding?.resolveScene).toBe(second.binding?.resolveScene);
    expect(sameMonitorBinding(first.binding, second.binding)).toBe(true);
    const moved = deriveNleMonitorBinding(
      snapshotFixture(SMOKE_SHAPE, 12),
      availableDisposition(),
      leaseClient,
    );
    expect(sameMonitorBinding(first.binding, moved.binding)).toBe(false);
    if (!first.binding) throw new Error("fixture binding unavailable");
    const changedCapability = {
      ...first.binding,
      capability: {
        ...first.binding.capability,
        frameObserver: "event_fallback" as const,
      },
    };
    expect(sameMonitorBinding(first.binding, changedCapability)).toBe(false);
  });

  it("degrades to the labeled selected-source fallback or unavailable by the disposition", () => {
    const unavailable = deriveNleMonitorBinding(
      snapshot,
      unavailableDisposition(),
      leaseClient,
    );
    expect(unavailable.mode).toBe("unavailable");
    expect(unavailable.binding).toBeNull();
    const fallback = deriveNleMonitorBinding(
      snapshot,
      {
        ...availableDisposition(),
        status: "qualification_required",
        blocker: "qualification_required",
        fallback: "selected_source_only",
      },
      leaseClient,
    );
    expect(fallback.mode).toBe("selected_source_only");
    expect(fallback.binding).toBeNull();
    expect(
      deriveNleMonitorBinding(undefined, availableDisposition(), leaseClient)
        .mode,
    ).toBe("unavailable");
  });
});

describe("M25-16 composition monitor", () => {
  const snapshot = snapshotFixture(SMOKE_SHAPE);

  it("owns one canvas, opens once and delegates every transport control to the session", async () => {
    const monitor = monitorHarness();
    const timeline = timelineBridgeHarness();
    const binding = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const onFrame = vi.fn();
    const channel = createMonitorStatusChannel();
    const view = render(
      <>
        <NleMonitorChips locale="en" channel={channel} />
        <NleMonitor
          locale="en"
          monitor={binding}
          selectedClip={undefined}
          onFrame={onFrame}
          statusChannel={channel}
          bindTimelineTransport={timeline.bind}
        />
      </>,
    );
    expect(monitor.created).toHaveLength(1);
    expect(monitor.session.open).toHaveBeenCalledTimes(1);
    expect(view.container.querySelectorAll("canvas")).toHaveLength(1);
    expect(view.container.querySelector("video,audio")).toBeNull();
    const control = (id: string) =>
      view.container.querySelector<HTMLButtonElement>(
        `[data-h3-nle-control="${id}"]`,
      )!;
    // Opening: nothing is enabled, and M25-45 offers recovery only where it applies.
    expect(control("transport.play").disabled).toBe(true);
    expect(control("transport.step_forward").disabled).toBe(true);
    expect(control("transport.recover")).toBeNull();
    act(() => {
      monitor.publish({ composition: compositionStatus("paused"), frame: 12 });
    });
    expect(onFrame).toHaveBeenLastCalledWith(12);
    expect(control("transport.play").disabled).toBe(false);
    // M25-45: one play/pause button, named for the action it performs now.
    expect(control("transport.pause")).toBeNull();
    fireEvent.click(control("transport.play"));
    expect(monitor.session.play).toHaveBeenCalledTimes(1);
    fireEvent.click(control("transport.step_forward"));
    expect(monitor.session.step).toHaveBeenLastCalledWith(1);
    fireEvent.click(control("transport.step_back"));
    expect(monitor.session.step).toHaveBeenLastCalledWith(-1);
    expect(
      view.container.querySelector('[data-h3-nle-control="transport.seek"]'),
    ).toBeNull();
    timeline.seek(240);
    expect(monitor.session.seek).toHaveBeenLastCalledWith(240);
    act(() => {
      monitor.publish({ composition: compositionStatus("playing") });
    });
    expect(control("transport.play")).toBeNull();
    expect(control("transport.pause").disabled).toBe(false);
    fireEvent.click(control("transport.pause"));
    expect(monitor.session.pause).toHaveBeenCalledTimes(1);
    act(() => {
      monitor.publish({
        composition: compositionStatus("blocked", "source_unavailable"),
      });
    });
    expect(control("transport.recover").disabled).toBe(false);
    fireEvent.click(control("transport.recover"));
    expect(monitor.session.recover).toHaveBeenCalledTimes(1);
    // The status itself now reads in the chrome bar, published through the channel.
    expect(
      view.container.querySelector('[data-h3-nle-status="monitor"]')!
        .textContent,
    ).not.toBe("");
    view.unmount();
    expect(monitor.dispose).toHaveBeenCalledTimes(1);
    expect(onFrame).toHaveBeenLastCalledWith(null);
  });

  it("routes one completed monitor gesture through one transform command callback", async () => {
    const rect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue({
        x: 0,
        y: 0,
        left: 0,
        top: 0,
        right: 960,
        bottom: 540,
        width: 960,
        height: 540,
        toJSON: () => ({}),
      });
    try {
      const monitor = monitorHarness();
      const binding = deriveNleMonitorBinding(
        snapshot,
        availableDisposition(),
        leaseClient,
      );
      const commit = vi.fn(async () => undefined);
      const selected = snapshot.clips[0]!;
      const view = render(
        <NleMonitor
          locale="en"
          monitor={binding}
          selectedClip={selected}
          onFrame={() => undefined}
          transformEditable
          transformAuthority="11:fixture:accepted"
          onTransformCommit={commit}
        />,
      );
      act(() => {
        monitor.publish({
          composition: compositionStatus("paused"),
          frame: 12,
        });
      });
      const move = await vi.waitFor(() => {
        const control = view.container.querySelector<HTMLButtonElement>(
          '[data-h3-nle-transform-handle="move"]',
        );
        expect(control).not.toBeNull();
        return control!;
      });
      fireEvent.pointerDown(move, {
        pointerId: 8,
        clientX: 480,
        clientY: 270,
      });
      fireEvent.pointerMove(move, {
        pointerId: 8,
        clientX: 576,
        clientY: 324,
      });
      fireEvent.pointerUp(move, {
        pointerId: 8,
        clientX: 576,
        clientY: 324,
      });
      expect(monitor.session.pause).not.toHaveBeenCalled();
      expect(monitor.session.previewVisualTransform).toHaveBeenCalledWith(
        selected.clipId,
        expect.objectContaining({
          position_x_bp: 1_000,
          position_y_bp: 1_000,
        }),
      );
      expect(commit).toHaveBeenCalledTimes(1);
    } finally {
      rect.mockRestore();
    }
  });

  it("accepts ruler frame targets without re-pausing an already paused owner", async () => {
    const monitor = monitorHarness();
    const timeline = timelineBridgeHarness();
    const binding = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const view = render(
      <NleMonitor
        locale="en"
        monitor={binding}
        selectedClip={undefined}
        onFrame={vi.fn()}
        bindTimelineTransport={timeline.bind}
      />,
    );
    act(() => {
      monitor.publish({ composition: compositionStatus("paused"), frame: 12 });
    });
    await timeline.pause();
    expect(monitor.session.pause).not.toHaveBeenCalled();
    for (const frame of [13, 14, 13, 12, 13, 37, 2879, 0]) timeline.seek(frame);
    expect(monitor.session.seek).toHaveBeenLastCalledWith(0);
    expect(currentTimecode(view.container)).toBe("00:00:00:00");
    view.unmount();
  });

  it("waits for an in-flight Play before pausing a scrub whose snapshot is still paused", async () => {
    const monitor = monitorHarness();
    const timeline = timelineBridgeHarness();
    const binding = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const view = render(
      <NleMonitor
        locale="en"
        monitor={binding}
        selectedClip={undefined}
        onFrame={vi.fn()}
        bindTimelineTransport={timeline.bind}
      />,
    );
    act(() => {
      monitor.publish({ composition: compositionStatus("paused"), frame: 24 });
    });
    let settlePlay!: () => void;
    vi.mocked(monitor.session.play).mockImplementationOnce(
      () => new Promise<void>((resolve) => (settlePlay = resolve)),
    );
    fireEvent.click(
      view.container.querySelector<HTMLElement>(
        '[data-h3-nle-control="transport.play"]',
      )!,
    );
    const pausing = timeline.pause();
    expect(monitor.session.pause).not.toHaveBeenCalled();
    settlePlay();
    await pausing;
    expect(monitor.session.pause).toHaveBeenCalledOnce();
    view.unmount();
  });

  it("keeps the timeline transport binding stable across frame and status renders", () => {
    const monitor = monitorHarness();
    const binding = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const bindTimelineTransport = vi.fn(() => vi.fn());
    const view = render(
      <NleMonitor
        locale="en"
        monitor={binding}
        selectedClip={undefined}
        onFrame={vi.fn()}
        bindTimelineTransport={bindTimelineTransport}
      />,
    );
    expect(bindTimelineTransport).toHaveBeenCalledTimes(1);
    act(() => {
      // The opening -> available boundary legitimately republishes capability once.
      monitor.publish({ composition: compositionStatus("paused"), frame: 24 });
    });
    expect(bindTimelineTransport).toHaveBeenCalledTimes(2);
    act(() => {
      monitor.publish({ composition: compositionStatus("playing"), frame: 25 });
      monitor.publish({ composition: compositionStatus("paused"), frame: 26 });
    });
    expect(bindTimelineTransport).toHaveBeenCalledTimes(2);
    view.unmount();
  });

  it("queues play behind a seek the user has already asked for", async () => {
    // M25-21 B3-D29: the session judges the terminal endpoint by the frame actually presented,
    // so a Play issued while a seek away from the last frame is still in flight is refused as
    // "already at the end" and silently republishes `paused` -- the button does nothing until it
    // is pressed again. "Home, then Play" at the end of a composition is exactly that sequence,
    // and it cost a five-minute playback workload its run. The ordering is owned here, where the
    // two user intents are sequenced; the runtime keeps superseding rather than serializing.
    const monitor = monitorHarness();
    const timeline = timelineBridgeHarness();
    const binding = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const view = render(
      <NleMonitor
        locale="en"
        monitor={binding}
        selectedClip={undefined}
        onFrame={vi.fn()}
        bindTimelineTransport={timeline.bind}
      />,
    );
    const control = (id: string) =>
      view.container.querySelector<HTMLButtonElement>(
        `[data-h3-nle-control="${id}"]`,
      )!;
    act(() => {
      monitor.publish({
        composition: compositionStatus("paused"),
        frame: 2879,
      });
    });
    let settleSeek!: () => void;
    vi.mocked(monitor.session.seek).mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          settleSeek = resolve;
        }),
    );
    timeline.seek(0);
    expect(monitor.session.seek).toHaveBeenLastCalledWith(0);
    fireEvent.click(control("transport.play"));
    expect(monitor.session.play).not.toHaveBeenCalled();
    await act(async () => {
      settleSeek();
    });
    expect(monitor.session.play).toHaveBeenCalledTimes(1);
    view.unmount();
  });

  it("steps the frame buttons from a seek the user has already asked for", async () => {
    // M25-44 B-M2544-05: the session's step pauses first, and a pause while a move is in flight
    // supersedes it with a seek back to the last presented frame. "Home, then Next frame" before
    // the Home was presented cancelled the Home and stepped from the old position (58 -> 59). The
    // buttons follow the slider's rule (B3-D10): while a seek is requested, step from it.
    const monitor = monitorHarness();
    const timeline = timelineBridgeHarness();
    const binding = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const view = render(
      <NleMonitor
        locale="en"
        monitor={binding}
        selectedClip={undefined}
        onFrame={vi.fn()}
        bindTimelineTransport={timeline.bind}
      />,
    );
    const control = (id: string) =>
      view.container.querySelector<HTMLElement>(
        `[data-h3-nle-control="transport.${id}"]`,
      )!;
    act(() => {
      monitor.publish({
        composition: compositionStatus("paused"),
        frame: 58,
      });
    });
    let settleSeek!: () => void;
    vi.mocked(monitor.session.seek).mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          settleSeek = resolve;
        }),
    );
    timeline.seek(0);
    expect(monitor.session.seek).toHaveBeenLastCalledWith(0);
    fireEvent.click(control("step_forward"));
    expect(monitor.session.step).not.toHaveBeenCalled();
    expect(monitor.session.seek).toHaveBeenLastCalledWith(1);
    expect(currentTimecode(view.container)).toBe("00:00:00:01");
    fireEvent.click(control("step_forward"));
    expect(monitor.session.seek).toHaveBeenLastCalledWith(2);
    fireEvent.click(control("step_back"));
    expect(monitor.session.seek).toHaveBeenLastCalledWith(1);
    await act(async () => {
      settleSeek();
    });
    expect(currentTimecode(view.container)).toBe("00:00:02:10");
    // With no seek requested, a step is the session's own pause-and-step.
    act(() => {
      monitor.publish({
        composition: compositionStatus("paused"),
        frame: 1,
      });
    });
    fireEvent.click(control("step_forward"));
    expect(monitor.session.step).toHaveBeenCalledExactlyOnceWith(1);
    // Playback owns no requested frame: the step pauses through the session.
    act(() => {
      monitor.publish({
        composition: compositionStatus("playing"),
        frame: 30,
      });
    });
    fireEvent.click(control("step_back"));
    expect(monitor.session.step).toHaveBeenLastCalledWith(-1);
    view.unmount();
  });

  it.each([
    "trim pause",
    "seek",
    "step",
    "recovery",
    "replacement effect",
    "replacement trigger",
    "unmount",
  ])("discards queued play after a newer %s", async (newerIntent) => {
    const monitor = monitorHarness();
    const timeline = timelineBridgeHarness();
    const binding = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const bindPause = vi.fn();
    const bindReplace = vi.fn();
    const props = {
      locale: "en" as const,
      monitor: binding,
      selectedClip: undefined,
      onFrame: vi.fn(),
      bindPause,
      bindReplace,
      bindTimelineTransport: timeline.bind,
    };
    const view = render(<NleMonitor {...props} />);
    act(() => {
      monitor.publish({
        composition: compositionStatus("paused"),
        frame: 2879,
      });
    });
    let settleSeek!: () => void;
    vi.mocked(monitor.session.seek).mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          settleSeek = resolve;
        }),
    );
    const control = (id: string) =>
      view.container.querySelector<HTMLElement>(
        `[data-h3-nle-control="transport.${id}"]`,
      )!;
    timeline.seek(0);
    fireEvent.click(control("play"));
    expect(monitor.session.play).not.toHaveBeenCalled();
    if (newerIntent === "trim pause") {
      await act(async () => {
        await bindPause.mock.calls[0]![0]();
      });
      expect(monitor.session.pause).toHaveBeenCalledOnce();
    } else if (newerIntent === "seek") {
      timeline.seek(24);
    } else if (newerIntent === "step") {
      fireEvent.click(control("step_forward"));
    } else if (newerIntent === "recovery") {
      act(() => {
        monitor.publish({
          composition: compositionStatus("blocked", "source_unavailable"),
        });
      });
      fireEvent.click(control("recover"));
    } else if (newerIntent.startsWith("replacement")) {
      const moved = deriveNleMonitorBinding(
        snapshotFixture(SMOKE_SHAPE, 12),
        availableDisposition(),
        leaseClient,
      );
      if (newerIntent === "replacement effect") {
        view.rerender(<NleMonitor {...props} monitor={moved} />);
      } else {
        act(() => {
          bindReplace.mock.calls[0]![0](moved.binding);
        });
      }
      expect(monitor.replace).toHaveBeenCalledOnce();
    } else {
      view.unmount();
    }
    await act(async () => {
      settleSeek();
    });
    expect(monitor.session.play).not.toHaveBeenCalled();
    view.unmount();
  });

  it.each([
    // A trim pause takes the transport: the requested frame is cleared at once.
    ["trim pause", "00:00:02:00"],
    // B-M2561-04: a replacement is the rebind to an accepted revision. The runtime holds the seek
    // for the new owner, so the requested frame stands until that seek settles; it used to be
    // cleared, and the ruler dropped the press.
    ["replacement", "00:00:01:00"],
  ])(
    "settles a seek requested in the same turn as %s",
    async (newerIntent, whilePending) => {
      const monitor = monitorHarness();
      const timeline = timelineBridgeHarness();
      const bindPause = vi.fn();
      const bindReplace = vi.fn();
      const binding = deriveNleMonitorBinding(
        snapshot,
        availableDisposition(),
        leaseClient,
      );
      const view = render(
        <NleMonitor
          locale="en"
          monitor={binding}
          selectedClip={undefined}
          onFrame={vi.fn()}
          bindPause={bindPause}
          bindReplace={bindReplace}
          bindTimelineTransport={timeline.bind}
        />,
      );
      act(() => {
        monitor.publish({
          composition: compositionStatus("paused"),
          frame: 48,
        });
      });
      let settleSeek!: () => void;
      vi.mocked(monitor.session.seek).mockImplementationOnce(
        () => new Promise<void>((resolve) => (settleSeek = resolve)),
      );
      act(() => {
        timeline.seek(24);
        if (newerIntent === "trim pause") bindPause.mock.calls[0]![0]();
        else {
          const moved = deriveNleMonitorBinding(
            snapshotFixture(SMOKE_SHAPE, 12),
            availableDisposition(),
            leaseClient,
          );
          bindReplace.mock.calls[0]![0](moved.binding);
        }
      });
      expect(currentTimecode(view.container)).toBe(whilePending);
      await act(async () => settleSeek());
      // Settled: the position is what the monitor presents (this harness still shows 48).
      expect(currentTimecode(view.container)).toBe("00:00:02:00");
      expect(monitor.session.play).not.toHaveBeenCalled();
    },
  );

  it("settles repeated seek targets by request identity rather than frame equality", async () => {
    const monitor = monitorHarness();
    const timeline = timelineBridgeHarness();
    const binding = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const view = render(
      <NleMonitor
        locale="en"
        monitor={binding}
        selectedClip={undefined}
        onFrame={vi.fn()}
        bindTimelineTransport={timeline.bind}
      />,
    );
    act(() => {
      monitor.publish({
        composition: compositionStatus("paused"),
        frame: 2879,
      });
    });
    const settle: Array<() => void> = [];
    vi.mocked(monitor.session.seek).mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          settle.push(resolve);
        }),
    );
    for (const value of [0, 24, 0]) timeline.seek(value);
    expect(settle).toHaveLength(3);
    await act(async () => {
      settle[0]!();
    });
    expect(currentTimecode(view.container)).toBe("00:00:00:00");
    await act(async () => {
      settle[1]!();
    });
    expect(currentTimecode(view.container)).toBe("00:00:00:00");
    await act(async () => {
      monitor.publish({ frame: 0 });
      settle[2]!();
    });
    expect(currentTimecode(view.container)).toBe("00:00:00:00");
    view.unmount();
  });

  it("shows the embedded-audio follower state read-only with no audio control", () => {
    const monitor = monitorHarness();
    const binding = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const channel = createMonitorStatusChannel();
    const view = render(
      <>
        <NleMonitorChips locale="en" channel={channel} />
        <NleMonitor
          locale="en"
          monitor={binding}
          selectedClip={undefined}
          onFrame={() => undefined}
          statusChannel={channel}
        />
      </>,
    );
    // M25-45: the audio state reads in the chrome bar, published through the channel.
    const audio = () =>
      view.container.querySelector<HTMLElement>(
        '[data-h3-nle-status="audio"]',
      )!;
    expect(audio().getAttribute("data-h3-nle-audio-state")).toBe("silent");
    act(() => {
      monitor.publish({
        audio: {
          schema: "h3.authoring.embedded_audio_follower_status.v1",
          policy_id: "primary_embedded_follow_video_v1",
          transport_epoch: 1,
          scene_fingerprint: null,
          owner_clip_id: "clip-0",
          state: "following",
          reason: "none",
          preview_capability: "available",
          final_render_capability: "not_evaluated",
        },
      });
    });
    expect(audio().getAttribute("data-h3-nle-audio-state")).toBe("following");
    expect(audio().getAttribute("data-h3-nle-audio-reason")).toBe("none");
    expect(audio().textContent).toContain("clip-0");
    expect(audio().querySelector("button,input,select")).toBeNull();
    expect(
      view.container.querySelector("[data-h3-nle-audio-track]"),
    ).toBeNull();
  });

  it("replaces the session binding when the snapshot identity moves and not otherwise", () => {
    const monitor = monitorHarness();
    const first = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const view = render(
      <NleMonitor
        locale="en"
        monitor={first}
        selectedClip={undefined}
        onFrame={() => undefined}
      />,
    );
    const same = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    view.rerender(
      <NleMonitor
        locale="en"
        monitor={same}
        selectedClip={undefined}
        onFrame={() => undefined}
      />,
    );
    expect(monitor.replace).not.toHaveBeenCalled();
    const moved = deriveNleMonitorBinding(
      snapshotFixture(SMOKE_SHAPE, 12),
      availableDisposition(),
      leaseClient,
    );
    view.rerender(
      <NleMonitor
        locale="en"
        monitor={moved}
        selectedClip={undefined}
        onFrame={() => undefined}
      />,
    );
    expect(monitor.replace).toHaveBeenCalledTimes(1);
    expect((monitor.replace.mock.calls[0] as unknown[])[0]).toBe(moved.binding);
    expect(monitor.created).toHaveLength(1);
  });

  // M25-21 B3-D56: the session owner that adopts an accepted revision starts the replacement in
  // the same task, so the adoption and the monitor's `opening` state are published in one React
  // commit. The binding effect must then NOT replace again for the binding the trigger already
  // started, or every accepted edit pays two native replacements.
  it("replaces once when the session starts the replacement before the binding arrives", () => {
    const monitor = monitorHarness();
    let trigger: ((binding: unknown) => void) | null = null;
    const first = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const view = render(
      <NleMonitor
        locale="en"
        monitor={first}
        selectedClip={undefined}
        onFrame={() => undefined}
        bindReplace={(replace) => {
          trigger = replace as ((binding: unknown) => void) | null;
        }}
      />,
    );
    expect(trigger).not.toBeNull();
    const moved = deriveNleMonitorBinding(
      snapshotFixture(SMOKE_SHAPE, 12),
      availableDisposition(),
      leaseClient,
    );
    act(() => trigger!(moved.binding));
    expect(monitor.replace).toHaveBeenCalledTimes(1);
    expect((monitor.replace.mock.calls[0] as unknown[])[0]).toBe(moved.binding);
    // The prop now catches up with what the session already started.
    view.rerender(
      <NleMonitor
        locale="en"
        monitor={moved}
        selectedClip={undefined}
        onFrame={() => undefined}
        bindReplace={(replace) => {
          trigger = replace as ((binding: unknown) => void) | null;
        }}
      />,
    );
    expect(monitor.replace).toHaveBeenCalledTimes(1);
    expect(monitor.created).toHaveLength(1);
    view.unmount();
    expect(trigger).toBeNull();
  });

  it("renders the labeled selected-source fallback with no canvas, no compositor and no session", () => {
    const monitor = monitorHarness();
    const selected = snapshot.clips[0]!;
    const channel = createMonitorStatusChannel();
    const view = render(
      <>
        <NleMonitorChips locale="en" channel={channel} />
        <NleMonitor
          locale="en"
          monitor={{ mode: "selected_source_only", binding: null }}
          selectedClip={selected}
          onFrame={() => undefined}
          statusChannel={channel}
        />
      </>,
    );
    expect(monitor.created).toHaveLength(0);
    expect(view.container.querySelector("canvas")).toBeNull();
    const region = view.container.querySelector(
      '[data-h3-nle-fallback="selected_source_only"]',
    )!;
    expect(region.getAttribute("data-h3-nle-status")).toBe("monitor");
    // No composition is bound, so the chrome bar claims no monitor status of its own and the
    // fallback region stays the one element that carries it.
    expect(
      view.container.querySelectorAll('[data-h3-nle-status="monitor"]'),
    ).toHaveLength(1);
    // M25-64 (row #27, R5): the fallback states itself in the picture overlay; the selected
    // clip's raw asset id is no longer printed.
    expect(region.textContent).toContain(
      "Composition monitor unavailable; the preview shows the selected clip's source only.",
    );
    expect(region.textContent).not.toContain(selected.assetId!);
    expect(view.container.querySelector("[data-h3-nle-control]")).toBeNull();
    expect(
      view.container
        .querySelector('[data-h3-nle-status="audio"]')!
        .getAttribute("data-h3-nle-audio-state"),
    ).toBe("silent");
  });

  it("opens only the explicit legacy source preview with its accepted revision identity", async () => {
    const projection = projectionFixture();
    const clip = projection.timeline.clips[0]!;
    const selected = {
      ...snapshot.clips[0]!,
      clipId: clip.clipId,
      assetId: clip.assetId,
    };
    const open = vi.fn(() => new Promise<never>(() => undefined));
    const view = render(
      <NleMonitor
        locale="en"
        monitor={{ mode: "selected_source_only", binding: null }}
        selectedClip={selected}
        onFrame={() => undefined}
        sourceProjection={projection}
        openSourcePreview={open}
      />,
    );
    expect(open).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(
        view.getByRole("button", { name: "Selected source only" }),
      );
    });
    expect(open).toHaveBeenCalledTimes(1);
    expect(open.mock.calls[0]).toEqual([
      expect.objectContaining({
        workspaceHandle: projection.workspaceHandle,
        clipId: clip.clipId,
        referenceRevision: projection.reference.revision,
        timelineRevision: projection.timeline.revision,
        timelineContentFingerprint: projection.timeline.contentFingerprint,
      }),
      expect.any(AbortSignal),
    ]);
    view.unmount();
    expect(
      (open.mock.calls[0] as unknown as [unknown, AbortSignal])[1].aborted,
    ).toBe(true);
  });

  it("does not add a preview-reset commit when publication and projection identity change while closed", () => {
    const ctx = monitorHarness();
    const projection = projectionFixture();
    const selected = snapshot.clips[0]!;
    const monitor = deriveNleMonitorBinding(
      snapshot,
      availableDisposition(),
      leaseClient,
    );
    const commits: string[] = [];
    const view = render(
      <Profiler id="monitor" onRender={(_id, phase) => commits.push(phase)}>
        <NleMonitor
          locale="en"
          monitor={monitor}
          selectedClip={selected}
          onFrame={() => undefined}
          sourceProjection={projection}
        />
      </Profiler>,
    );
    commits.length = 0;
    act(() => {
      ctx.publish({ composition: compositionStatus("paused") });
      view.rerender(
        <Profiler id="monitor" onRender={(_id, phase) => commits.push(phase)}>
          <NleMonitor
            locale="en"
            monitor={monitor}
            selectedClip={selected}
            onFrame={() => undefined}
            sourceProjection={{ ...projection }}
          />
        </Profiler>,
      );
    });
    expect(commits).toHaveLength(1);
  });

  it("invalidates a queued source open, fences its late completion, and permits a fresh open", async () => {
    const projection = projectionFixture();
    const clip = projection.timeline.clips[0]!;
    const selected = {
      ...snapshot.clips[0]!,
      clipId: clip.clipId,
      assetId: clip.assetId,
    };
    let finish:
      ((value: { blob: Blob; audioDisposition: "absent" }) => void) | null =
      null;
    const open = vi.fn(
      () =>
        new Promise<{ blob: Blob; audioDisposition: "absent" }>((resolve) => {
          finish = resolve;
        }),
    );
    const renderMonitor = (sourceProjection: typeof projection) => (
      <NleMonitor
        locale="en"
        monitor={{ mode: "selected_source_only", binding: null }}
        selectedClip={selected}
        onFrame={() => undefined}
        sourceProjection={sourceProjection}
        openSourcePreview={open}
      />
    );
    const view = render(renderMonitor(projection));
    await act(async () => {
      fireEvent.click(
        view.getByRole("button", { name: "Selected source only" }),
      );
    });
    expect(open).toHaveBeenCalledTimes(1);
    const firstSignal = (
      open.mock.calls[0] as unknown as [unknown, AbortSignal]
    )[1];
    expect(firstSignal.aborted).toBe(false);

    const replacement = {
      ...projection,
      timeline: {
        ...projection.timeline,
        revision: projection.timeline.revision + 1,
        contentFingerprint: `sha256:${"9".repeat(64)}`,
      },
    };
    await act(async () => view.rerender(renderMonitor(replacement)));
    expect(firstSignal.aborted).toBe(true);
    expect(view.container.querySelector("section.h3a-preview")).toBeNull();

    await act(async () => {
      finish?.({ blob: new Blob(["late"]), audioDisposition: "absent" });
      await Promise.resolve();
    });
    expect(view.container.querySelector("section.h3a-preview")).toBeNull();

    const callsBeforeFreshOpen = open.mock.calls.length;
    await act(async () => {
      fireEvent.click(
        view.getByRole("button", { name: "Selected source only" }),
      );
    });
    expect(open.mock.calls.length).toBeGreaterThan(callsBeforeFreshOpen);
    const freshSignal = (
      open.mock.calls.at(-1) as unknown as [unknown, AbortSignal]
    )[1];
    expect(freshSignal.aborted).toBe(false);
    expect(view.container.querySelector("section.h3a-preview")).not.toBeNull();
  });

  it("does not carry a same-batch source-open intent across a projection replacement", async () => {
    const projection = projectionFixture();
    const clip = projection.timeline.clips[0]!;
    const selected = {
      ...snapshot.clips[0]!,
      clipId: clip.clipId,
      assetId: clip.assetId,
    };
    const open = vi.fn(() => new Promise<never>(() => undefined));
    const renderMonitor = (sourceProjection: typeof projection) => (
      <NleMonitor
        locale="en"
        monitor={{ mode: "selected_source_only", binding: null }}
        selectedClip={selected}
        onFrame={() => undefined}
        sourceProjection={sourceProjection}
        openSourcePreview={open}
      />
    );
    const view = render(renderMonitor(projection));
    const replacement = {
      ...projection,
      timeline: {
        ...projection.timeline,
        revision: projection.timeline.revision + 1,
        contentFingerprint: `sha256:${"8".repeat(64)}`,
      },
    };

    // One React batch reproduces the original race: the click queues an open while the same
    // accepted update invalidates the projection that authorized it. No stale child may mount.
    act(() => {
      fireEvent.click(
        view.getByRole("button", { name: "Selected source only" }),
      );
      view.rerender(renderMonitor(replacement));
    });
    expect(open).not.toHaveBeenCalled();
    expect(view.container.querySelector("section.h3a-preview")).toBeNull();

    await act(async () => {
      fireEvent.click(
        view.getByRole("button", { name: "Selected source only" }),
      );
    });
    expect(open).toHaveBeenCalledTimes(1);
    const freshSignal = (
      open.mock.calls[0] as unknown as [unknown, AbortSignal]
    )[1];
    expect(freshSignal.aborted).toBe(false);
    expect(view.container.querySelector("section.h3a-preview")).not.toBeNull();
  });
});

// M25-64 (rows #26 and #27, A64-1): the monitor's header states the accepted output's facts, and
// the picture area says why it has no picture in one centred overlay with at most one action.
describe("M25-64 monitor header and overlays", () => {
  const snapshot = snapshotFixture(SMOKE_SHAPE);
  const facts = `${snapshot.output.width as number} × ${snapshot.output.height as number} · 24 fps`;

  function header(container: HTMLElement) {
    return container.querySelector<HTMLElement>("[data-h3-nle-monitor-header]");
  }

  function overlay(container: HTMLElement) {
    return container.querySelector<HTMLElement>("[data-h3-nle-overlay]");
  }

  it("heads the composition monitor with Preview and the output's size and rate", () => {
    monitorHarness();
    const view = render(
      <NleMonitor
        locale="en"
        monitor={deriveNleMonitorBinding(
          snapshot,
          availableDisposition(),
          leaseClient,
        )}
        selectedClip={undefined}
        onFrame={() => undefined}
      />,
    );
    const bar = header(view.container)!;
    expect(bar.querySelector("[data-h3-nle-monitor-title]")!.textContent).toBe(
      "Preview",
    );
    expect(bar.querySelector("[data-h3-nle-monitor-facts]")!.textContent).toBe(
      facts,
    );
    // The header comes first, the picture area second and the transport last.
    const section = view.container.querySelector(".h3-nle-monitor")!;
    expect(
      [...section.children].map((child) => child.className.split(" ")[0]),
    ).toEqual(["h3-nle-monitor-header", "h3-nle-picture", "h3-nle-transport"]);
  });

  it("heads the fallback monitor from the snapshot's output and omits absent facts", () => {
    const view = render(
      <NleMonitor
        locale="zh-TW"
        monitor={{ mode: "unavailable", binding: null }}
        output={snapshot.output}
        selectedClip={undefined}
        onFrame={() => undefined}
      />,
    );
    const bar = header(view.container)!;
    expect(bar.querySelector("[data-h3-nle-monitor-title]")!.textContent).toBe(
      "預覽",
    );
    expect(bar.querySelector("[data-h3-nle-monitor-facts]")!.textContent).toBe(
      facts,
    );
    view.rerender(
      <NleMonitor
        locale="zh-TW"
        monitor={{ mode: "unavailable", binding: null }}
        selectedClip={undefined}
        onFrame={() => undefined}
      />,
    );
    expect(
      header(view.container)!.querySelector("[data-h3-nle-monitor-facts]"),
    ).toBeNull();
  });

  it("shows the empty-timeline overlay with a title, the + and drag hint, and no action", () => {
    monitorHarness();
    const view = render(
      <NleMonitor
        locale="en"
        monitor={deriveNleMonitorBinding(
          snapshot,
          availableDisposition(),
          leaseClient,
        )}
        selectedClip={undefined}
        emptyTimeline
        onFrame={() => undefined}
      />,
    );
    const empty = overlay(view.container)!;
    expect(empty.getAttribute("data-h3-nle-overlay")).toBe("empty");
    expect(empty.getAttribute("data-h3-nle-empty")).toBe("timeline");
    expect(empty.getAttribute("role")).toBe("status");
    expect(empty.closest(".h3-nle-picture")).not.toBeNull();
    expect(
      empty.querySelector("[data-h3-nle-overlay-title]")!.textContent,
    ).toBe("Nothing on the timeline yet");
    const hint = empty.querySelector(
      "[data-h3-nle-overlay-detail]",
    )!.textContent!;
    expect(hint).toContain("+");
    expect(hint).toContain("Media");
    expect(hint).toContain("drag");
    expect(empty.querySelectorAll("button")).toHaveLength(0);
    expect(empty.querySelector("svg[aria-hidden='true']")).not.toBeNull();
  });

  it("shows the unavailable overlay with its plain reason and Retry on the one recover path", () => {
    const monitor = monitorHarness();
    const view = render(
      <NleMonitor
        locale="en"
        monitor={deriveNleMonitorBinding(
          snapshot,
          availableDisposition(),
          leaseClient,
        )}
        selectedClip={undefined}
        onFrame={() => undefined}
      />,
    );
    expect(overlay(view.container)).toBeNull();
    act(() => {
      monitor.publish({
        composition: compositionStatus("blocked", "canvas_unavailable"),
      });
    });
    const card = overlay(view.container)!;
    expect(card.getAttribute("data-h3-nle-overlay")).toBe("unavailable");
    expect(card.hasAttribute("data-h3-nle-recovery")).toBe(true);
    expect(card.getAttribute("role")).toBe("status");
    expect(card.querySelector("[data-h3-nle-overlay-title]")!.textContent).toBe(
      "Preview unavailable",
    );
    // M25-64 (A64-6): a plain sentence under the title, not the status sentence that repeated
    // "unavailable"; the status sentence stays the diagnostics text.
    expect(
      card.querySelector("[data-h3-nle-overlay-detail]")!.textContent,
    ).toBe("The browser could not draw the picture.");
    const buttons = card.querySelectorAll("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]!.getAttribute("data-h3-nle-control")).toBe(
      "transport.recover",
    );
    expect(buttons[0]!.textContent).toBe("Retry");
    fireEvent.click(buttons[0]!);
    expect(monitor.session.recover).toHaveBeenCalledTimes(1);
    // The notes the overlays replace are gone.
    expect(view.container.querySelector(".h3-nle-picture-note")).toBeNull();
  });

  it("M25-64 (A64-6): gives every blocker a plain overlay reason in three locales, the status unchanged", () => {
    const blockers = [
      ["source_unavailable", "A clip's source could not be read."],
      ["canvas_unavailable", "The browser could not draw the picture."],
      ["invalid_contract", "This composition can't be previewed."],
      [
        "resource_limit",
        "The preview needs more resources than the browser allows.",
      ],
      ["cleanup_pending", "The previous preview is still closing."],
    ] as const;
    for (const [blocker, sentence] of blockers) {
      const composition = compositionStatus("blocked", blocker);
      expect(monitorUnavailableReason(nleCopy("en"), composition)).toBe(
        sentence,
      );
      expect(monitorStatusText(nleCopy("en"), composition)).toMatch(
        /^Monitor (unavailable: .+|cleanup is pending)\.$/u,
      );
      for (const locale of ["zh-TW", "zh-CN"] as const) {
        const reason = monitorUnavailableReason(nleCopy(locale), composition);
        expect(reason).toMatch(/。$/u);
        expect(reason).not.toBe(
          monitorStatusText(nleCopy(locale), composition),
        );
      }
    }
    // A blocked status without a named blocker reads as the source case, as the status does.
    expect(
      monitorUnavailableReason(nleCopy("en"), compositionStatus("blocked")),
    ).toBe("A clip's source could not be read.");
  });

  it("states the fallback modes in the same overlay without a raw asset id", () => {
    const selected = snapshot.clips[0]!;
    const view = render(
      <NleMonitor
        locale="en"
        monitor={{ mode: "unavailable", binding: null }}
        selectedClip={selected}
        onFrame={() => undefined}
      />,
    );
    const card = overlay(view.container)!;
    expect(card.getAttribute("data-h3-nle-overlay")).toBe("unavailable");
    expect(card.querySelector("[data-h3-nle-overlay-title]")!.textContent).toBe(
      "Preview unavailable",
    );
    expect(
      card.querySelector("[data-h3-nle-overlay-detail]")!.textContent,
    ).toBe("Composition monitor unavailable.");
    expect(card.querySelectorAll("button")).toHaveLength(0);
    expect(view.container.textContent).not.toContain(selected.assetId!);
    view.rerender(
      <NleMonitor
        locale="en"
        monitor={{ mode: "unavailable", binding: null }}
        selectedClip={undefined}
        emptyTimeline
        onFrame={() => undefined}
      />,
    );
    expect(overlay(view.container)!.getAttribute("data-h3-nle-overlay")).toBe(
      "empty",
    );
    expect(view.container.textContent).not.toContain(
      "Composition monitor unavailable.",
    );
  });
});

// B-M2564-12: during a rebind the transport keeps its frame (B-M2561-04) while the new owner shows
// frame 0 first. The canvas names the frame it shows, which the host journeys wait on before they
// read its pixels; the transport's kept frame is the ruler's.
describe("M25-64 B-M2564-12 monitor picture", () => {
  it("names the frame on the canvas, not the transport's kept frame", () => {
    const monitor = monitorHarness();
    const binding = deriveNleMonitorBinding(
      snapshotFixture(SMOKE_SHAPE),
      availableDisposition(),
      leaseClient,
    );
    const view = render(
      <NleMonitor
        locale="en"
        monitor={binding}
        selectedClip={undefined}
        onFrame={() => undefined}
      />,
    );
    const canvas = view.container.querySelector(
      '[data-h3-nle-canvas="composition"]',
    )!;
    act(() => {
      monitor.publish({
        composition: compositionStatus("paused"),
        frame: 200,
        presentedFrame: 0,
      });
    });
    expect(canvas.getAttribute("data-h3-nle-presented-frame")).toBe("0");
    act(() => {
      monitor.publish({ presentedFrame: 200 });
    });
    expect(canvas.getAttribute("data-h3-nle-presented-frame")).toBe("200");
    act(() => {
      monitor.publish({ presentedFrame: null });
    });
    expect(canvas.hasAttribute("data-h3-nle-presented-frame")).toBe(false);
  });
});
