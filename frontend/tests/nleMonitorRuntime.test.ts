import { beforeEach, describe, expect, it, vi } from "vitest";

import { createNleMonitor } from "../src/runtime/nleWorkspaceRuntime";
import type {
  VisualCompositionBinding,
  VisualCompositionStatus,
} from "../src/runtime/visualCompositionSession";

const seam = vi.hoisted(() => ({ create: vi.fn(), follower: vi.fn() }));
vi.mock("../src/runtime/visualCompositionSession", () => ({
  createVisualCompositionSession: seam.create,
}));
vi.mock("../src/runtime/embeddedAudioFollower", () => ({
  createEmbeddedAudioFollower: seam.follower,
}));

function subject(retained = false) {
  const listeners = new Set<() => void>();
  const frames = new Set<() => void>();
  let status: VisualCompositionStatus = {
    status: "paused",
    blocker: null,
    browserPreviewOnly: true,
    durationFrames: 24,
  };
  let frame = 0;
  const pending: { resolve(): void; reject(error: Error): void }[] = [];
  const session = {
    canRebind: vi.fn(() => retained),
    getSnapshot: () => status,
    getPresentation: () => ({ status: "presented", frame }),
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    subscribeFrame: (listener: () => void) => {
      frames.add(listener);
      return () => frames.delete(listener);
    },
    replace: vi.fn(
      (
        _binding: VisualCompositionBinding,
        initialFrame?: number | (() => number),
      ) =>
        new Promise<void>((resolve, reject) =>
          pending.push({
            resolve: () => {
              if (initialFrame !== undefined)
                frame =
                  typeof initialFrame === "function"
                    ? initialFrame()
                    : initialFrame;
              resolve();
            },
            reject,
          }),
        ),
    ),
    open: vi.fn(
      (_initialFrame?: number | (() => number)) =>
        new Promise<void>((resolve, reject) =>
          pending.push({ resolve, reject }),
        ),
    ),
    close: vi.fn(async () => undefined),
    pause: vi.fn(async () => undefined),
    seek: vi.fn(async (_frame: number): Promise<void> => undefined),
    play: vi.fn(async () => undefined),
    step: vi.fn(async (_direction: -1 | 1): Promise<void> => undefined),
  };
  seam.create.mockReturnValue(session);
  const binding = {
    snapshot: { output: { durationFrames: 24 } },
  } as unknown as VisualCompositionBinding;
  const monitor = createNleMonitor(document.createElement("canvas"), binding);
  const observer = vi.fn();
  monitor.subscribe(observer);
  return {
    monitor,
    observer,
    pending,
    binding,
    session,
    replacementTarget() {
      const target = session.replace.mock.calls.at(-1)?.[1];
      return typeof target === "function" ? target() : target;
    },
    openingTarget() {
      const target = session.open.mock.calls.at(-1)?.[0];
      return typeof target === "function" ? target() : target;
    },
    publish(nextStatus = status, nextFrame = frame) {
      status = nextStatus;
      frame = nextFrame;
      for (const listener of [...listeners, ...frames]) listener();
    },
  };
}

beforeEach(() => seam.create.mockReset());

describe("NLE monitor replacement publication", () => {
  it("keeps the follower status through retained replacement and keeps absent audio null through fallback", async () => {
    for (const retained of [true, false]) {
      const ctx = subject(retained);
      if (retained) {
        const audio = { state: "following", reason: "none" };
        seam.follower.mockReturnValue({
          runtime: {},
          status: () => audio,
          subscribe: () => () => undefined,
        });
        seam.create.mock.calls.at(-1)![0].runtimeFactory({ leaseClient: {} });
        ctx.publish();
        expect(ctx.monitor.status().audio).toBe(audio);
      }
      const before = ctx.monitor.status().audio;
      const replacing = ctx.monitor.replace(ctx.binding);
      expect(ctx.monitor.status().audio).toBe(before);
      ctx.pending[0]!.resolve();
      await replacing;
      await ctx.monitor.dispose();
    }
  });
  it("gives the audio follower the monitor's shared playback scheduler", async () => {
    const h = subject();
    const scheduler = { acquirePlayback: vi.fn() };
    const follower = {
      runtime: {},
      subscribe: vi.fn(() => () => undefined),
      status: () => null,
    };
    seam.follower.mockReturnValue(follower);
    const monitor = createNleMonitor(
      document.createElement("canvas"),
      h.binding,
      {
        playbackScheduler: scheduler,
      },
    );
    const factory = seam.create.mock.calls.at(-1)![0].runtimeFactory;
    const runtimeOptions = { leaseClient: {} };
    expect(factory(runtimeOptions)).toBe(follower.runtime);
    expect(seam.follower).toHaveBeenLastCalledWith({
      ...runtimeOptions,
      playbackScheduler: scheduler,
    });
    await monitor.dispose();
    await h.monitor.dispose();
  });

  it("keeps the presented nonzero frame and paused status during a compatible rebind", async () => {
    const ctx = subject(true);
    ctx.publish(undefined, 12);
    const replacing = ctx.monitor.replace(ctx.binding);
    expect(ctx.monitor.status()).toMatchObject({
      composition: { status: "paused" },
      frame: 12,
      presentedFrame: 12,
      rebinding: true,
    });
    await ctx.monitor.session.seek(17);
    expect(ctx.monitor.status()).toMatchObject({
      composition: { status: "paused" },
      frame: 17,
      presentedFrame: 12,
      rebinding: true,
    });
    expect(ctx.session.seek).not.toHaveBeenCalled();
    ctx.pending[0]!.resolve();
    await replacing;
    expect(ctx.monitor.status()).toMatchObject({
      composition: { status: "paused" },
      frame: 17,
      presentedFrame: 17,
    });
    expect(ctx.session.seek).not.toHaveBeenCalled();
    expect(ctx.session.close).not.toHaveBeenCalled();
  });

  it("forwards the late-bound retained frame through initial opening", async () => {
    const ctx = subject();
    let retainedFrame = 12;
    const opening = ctx.monitor.session.open(() => retainedFrame);
    retainedFrame = 18;
    expect(ctx.openingTarget()).toBe(18);
    ctx.pending[0]!.resolve();
    await opening;
  });

  it("retains only the newest seek during initial opening until the owner settles", async () => {
    const ctx = subject();
    const opening = ctx.monitor.session.open();
    await ctx.monitor.session.seek(3);
    await ctx.monitor.session.seek(8);
    expect(ctx.session.seek).not.toHaveBeenCalled();
    ctx.pending[0]!.resolve();
    await opening;
    expect(ctx.session.seek).toHaveBeenCalledExactlyOnceWith(8);
  });

  it("carries deferred seek and pause to the latest binding after a failed open", async () => {
    const ctx = subject();
    const first = ctx.monitor.replace(ctx.binding);
    const failed = expect(first).rejects.toThrow("failed");
    await ctx.monitor.session.seek(8);
    await ctx.monitor.session.pause();
    const latest = ctx.monitor.replace(ctx.binding);
    ctx.pending[0]!.reject(new Error("failed"));
    await failed;
    expect(ctx.session.seek).not.toHaveBeenCalled();
    expect(ctx.session.pause).not.toHaveBeenCalled();
    ctx.pending[1]!.resolve();
    await latest;
    await Promise.resolve();
    expect(ctx.replacementTarget()).toBe(8);
    expect(ctx.session.seek).not.toHaveBeenCalled();
    expect(ctx.session.pause).toHaveBeenCalledTimes(1);
  });

  it("drops deferred seek when closed before initial opening settles", async () => {
    const ctx = subject();
    const opening = ctx.monitor.session.open();
    await ctx.monitor.session.seek(8);
    await ctx.monitor.dispose();
    ctx.pending[0]!.resolve();
    await opening;
    expect(ctx.session.seek).not.toHaveBeenCalled();
  });

  it("does not apply an older deferred pause after a fresh play supersedes a settling seek", async () => {
    const ctx = subject();
    let finishSeek!: () => void;
    ctx.session.seek.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishSeek = resolve;
        }),
    );
    const opening = ctx.monitor.session.open();
    await ctx.monitor.session.seek(8);
    await ctx.monitor.session.pause();
    ctx.pending[0]!.resolve();
    await opening;
    await ctx.monitor.session.play();
    finishSeek();
    await Promise.resolve();
    expect(ctx.session.play).toHaveBeenCalledTimes(1);
    expect(ctx.session.pause).not.toHaveBeenCalled();
  });

  it("retains a pause requested during media opening and applies it once the owner is ready", async () => {
    const ctx = subject();
    const first = ctx.monitor.replace(ctx.binding);
    await ctx.monitor.session.pause();
    // The opening owner is never paused mid-acquisition...
    expect(ctx.session.pause).not.toHaveBeenCalled();
    ctx.pending[0]!.resolve();
    await first;
    // ...but the retained intent is applied exactly once when it is ready, not dropped.
    expect(ctx.session.pause).toHaveBeenCalledTimes(1);
    await ctx.monitor.session.pause();
    expect(ctx.session.pause).toHaveBeenCalledTimes(2);
  });

  it("applies a retained pause only to the newest replacement, never to a superseded owner", async () => {
    const ctx = subject();
    const first = ctx.monitor.replace(ctx.binding);
    await ctx.monitor.session.pause();
    const latest = ctx.monitor.replace(ctx.binding);
    ctx.pending[0]!.resolve();
    await first;
    // The first owner settled while a newer binding was queued: nothing is paused yet.
    expect(ctx.session.pause).not.toHaveBeenCalled();
    ctx.pending[1]!.resolve();
    await latest;
    expect(ctx.session.pause).toHaveBeenCalledTimes(1);
  });

  it("drops a retained pause when the monitor is disposed before the owner is ready", async () => {
    const ctx = subject();
    const first = ctx.monitor.replace(ctx.binding);
    await ctx.monitor.session.pause();
    await ctx.monitor.dispose();
    ctx.pending[0]!.resolve();
    await first;
    expect(ctx.session.pause).not.toHaveBeenCalled();
  });
  it("settles initial media opening before applying a newer binding", async () => {
    const ctx = subject();
    const opening = ctx.monitor.session.open();
    const replacing = ctx.monitor.replace(ctx.binding);
    expect(ctx.session.replace).not.toHaveBeenCalled();
    ctx.pending[0]!.resolve();
    await opening;
    expect(ctx.session.replace).toHaveBeenCalledTimes(1);
    ctx.pending[1]!.resolve();
    await replacing;
  });

  it("continues the latest binding after a failed in-flight open", async () => {
    const ctx = subject();
    const first = ctx.monitor.replace(ctx.binding);
    const failed = expect(first).rejects.toThrow("failed");
    const latest = ctx.monitor.replace(ctx.binding);
    ctx.pending[0]!.reject(new Error("failed"));
    await failed;
    expect(ctx.session.replace).toHaveBeenCalledTimes(2);
    ctx.pending[1]!.resolve();
    await latest;
  });

  it("does not open queued bindings after immediate disposal", async () => {
    const ctx = subject();
    const first = ctx.monitor.replace(ctx.binding);
    const queued = ctx.monitor.replace(ctx.binding);
    await ctx.monitor.dispose();
    expect(ctx.session.close).toHaveBeenCalledTimes(1);
    ctx.pending[0]!.resolve();
    await Promise.all([first, queued]);
    expect(ctx.session.replace).toHaveBeenCalledTimes(1);
  });

  it("settles the in-flight media replacement before opening only the latest queued binding", async () => {
    const ctx = subject();
    const first = ctx.monitor.replace(ctx.binding);
    const middle = ctx.monitor.replace({ ...ctx.binding });
    const latestBinding = { ...ctx.binding };
    const latest = ctx.monitor.replace(latestBinding);
    expect(ctx.session.replace).toHaveBeenCalledTimes(1);
    ctx.pending[0]!.resolve();
    await first;
    await middle;
    expect(ctx.session.replace).toHaveBeenCalledTimes(2);
    expect(ctx.session.replace).toHaveBeenLastCalledWith(
      latestBinding,
      expect.any(Function),
    );
    ctx.pending[1]!.resolve();
    await latest;
  });

  // A superseding transport intent aborts the settling seek's native acquisition, while the
  // backend derivative build keeps its single claim: the replacement is refused `busy`.
  it("retains only the newest seek while an earlier seek is still settling", async () => {
    const ctx = subject();
    let finishSeek!: () => void;
    ctx.session.seek.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishSeek = resolve;
        }),
    );
    const first = ctx.monitor.session.seek(24);
    const settled: number[] = [];
    const middle = ctx.monitor.session.seek(48).then(() => settled.push(48));
    const latest = ctx.monitor.session.seek(84).then(() => settled.push(84));
    await Promise.resolve();
    expect(ctx.session.seek).toHaveBeenCalledExactlyOnceWith(24);
    // The slider steps from a requested frame until its seek settles: a retained seek must not
    // settle before the newest retained frame has actually been sought.
    expect(settled).toEqual([]);
    finishSeek();
    await first;
    await Promise.all([middle, latest]);
    expect(ctx.session.seek).toHaveBeenCalledTimes(2);
    expect(ctx.session.seek).toHaveBeenLastCalledWith(84);
    expect(settled).toEqual([48, 84]);
  });

  it("settles a seek retained behind an in-flight seek when the monitor is disposed", async () => {
    const ctx = subject();
    ctx.session.seek.mockImplementationOnce(
      () => new Promise<void>(() => undefined),
    );
    void ctx.monitor.session.seek(24);
    const retained = ctx.monitor.session.seek(48);
    await ctx.monitor.dispose();
    await retained;
    expect(ctx.session.seek).toHaveBeenCalledExactlyOnceWith(24);
  });

  it("applies a pause requested during a settling seek only after that seek settles", async () => {
    const ctx = subject();
    let finishSeek!: () => void;
    ctx.session.seek.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishSeek = resolve;
        }),
    );
    const first = ctx.monitor.session.seek(24);
    await ctx.monitor.session.pause();
    expect(ctx.session.pause).not.toHaveBeenCalled();
    finishSeek();
    await first;
    await Promise.resolve();
    expect(ctx.session.pause).toHaveBeenCalledTimes(1);
  });

  it("settles an in-flight seek before replacing its media owner", async () => {
    const ctx = subject();
    let finishSeek!: () => void;
    ctx.session.seek.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishSeek = resolve;
        }),
    );
    const first = ctx.monitor.session.seek(24);
    const replacing = ctx.monitor.replace(ctx.binding);
    await Promise.resolve();
    expect(ctx.session.replace).not.toHaveBeenCalled();
    finishSeek();
    await first;
    await vi.waitFor(() =>
      expect(ctx.session.replace).toHaveBeenCalledTimes(1),
    );
    ctx.pending[0]!.resolve();
    await replacing;
  });

  it("deduplicates session and frame notifications for one presented state", () => {
    const ctx = subject();
    ctx.publish(undefined, 1);
    expect(ctx.observer).toHaveBeenCalledTimes(1);
    const accepted = ctx.monitor.status();
    ctx.publish();
    expect(ctx.monitor.status()).toBe(accepted);
    expect(ctx.observer).toHaveBeenCalledTimes(1);
  });

  it("hides intermediate teardown states and ignores stale replacement completion", async () => {
    const ctx = subject();
    let logicalFrame = 0;
    const first = ctx.monitor.replace(ctx.binding);
    const second = ctx.monitor.replace(ctx.binding, () => logicalFrame);
    ctx.publish({
      status: "closed",
      blocker: null,
      browserPreviewOnly: true,
      durationFrames: 0,
    });
    ctx.pending[0]!.resolve();
    await first;
    expect(ctx.monitor.status().composition.status).toBe("opening");
    expect(ctx.observer).toHaveBeenCalledTimes(2);
    ctx.publish(
      {
        status: "paused",
        blocker: null,
        browserPreviewOnly: true,
        durationFrames: 24,
      },
      6,
    );
    logicalFrame = 6;
    ctx.pending[1]!.resolve();
    await second;
    expect(ctx.monitor.status().frame).toBe(6);
    expect(ctx.monitor.status().composition.status).toBe("paused");
    expect(ctx.observer).toHaveBeenCalledTimes(3);
  });

  it("publishes the accepted unavailable state when replacement fails", async () => {
    const ctx = subject();
    const replacement = ctx.monitor.replace(ctx.binding);
    ctx.publish({
      status: "blocked",
      blocker: "source_unavailable",
      browserPreviewOnly: true,
      durationFrames: 24,
    } as VisualCompositionStatus);
    ctx.pending[0]!.reject(new Error("replacement_unavailable"));
    await expect(replacement).rejects.toThrow("replacement_unavailable");
    expect(ctx.monitor.status().composition.status).toBe("blocked");
    expect(ctx.observer).toHaveBeenCalledTimes(2);
  });

  it("does not publish a replacement that completes after disposal", async () => {
    const ctx = subject();
    const replacement = ctx.monitor.replace(ctx.binding);
    await ctx.monitor.dispose();
    ctx.pending[0]!.resolve();
    await replacement;
    ctx.publish(undefined, 10);
    expect(ctx.observer).toHaveBeenCalledTimes(1);
    expect(ctx.session.close).toHaveBeenCalledTimes(1);
  });
});

// B-M2561-04: an accepted edit, a selection included, rebinds the monitor to the new revision.
// The transport keeps its position and its controls; what they send is held for the new owner.
describe("NLE monitor rebind keeps the transport (B-M2561-04)", () => {
  function moved(durationFrames: number) {
    return {
      snapshot: { output: { durationFrames } },
    } as unknown as VisualCompositionBinding;
  }

  it("keeps the presented frame and opens the new owner there before first paint", async () => {
    const ctx = subject();
    ctx.publish(undefined, 10);
    expect(ctx.monitor.status().frame).toBe(10);
    const replacement = ctx.monitor.replace(moved(24));
    expect(ctx.monitor.status()).toMatchObject({
      frame: 10,
      rebinding: true,
      composition: { status: "opening" },
    });
    // No bootstrap presentation may replace the logical position during the rebind.
    ctx.publish(undefined, 0);
    expect(ctx.replacementTarget()).toBe(10);
    ctx.pending[0]!.resolve();
    await replacement;
    expect(ctx.session.seek).not.toHaveBeenCalled();
    expect(ctx.monitor.status().frame).toBe(10);
    expect(ctx.monitor.status().rebinding).toBeUndefined();
    await Promise.resolve();
    ctx.publish(undefined, 10);
    expect(ctx.monitor.status().frame).toBe(10);
  });

  it("clamps the kept frame to the new duration", async () => {
    const ctx = subject();
    ctx.publish(undefined, 20);
    const replacement = ctx.monitor.replace(moved(6));
    expect(ctx.monitor.status().frame).toBe(5);
    expect(ctx.replacementTarget()).toBe(5);
    ctx.pending[0]!.resolve();
    await replacement;
    expect(ctx.session.seek).not.toHaveBeenCalled();
  });

  it("makes a seek held during the rebind the kept position", async () => {
    const ctx = subject();
    ctx.publish(undefined, 10);
    const replacement = ctx.monitor.replace(moved(24));
    await ctx.monitor.session.seek(3);
    expect(ctx.monitor.status().frame).toBe(3);
    expect(ctx.session.seek).not.toHaveBeenCalled();
    expect(ctx.replacementTarget()).toBe(3);
    ctx.pending[0]!.resolve();
    await replacement;
    expect(ctx.session.seek).not.toHaveBeenCalled();
  });

  it("B-M2564-04: publishes a seek held by a rebind that began before any frame was presented", async () => {
    // A selection accepted while the first owner is still opening starts a rebind with no kept
    // position. A ruler press then is held for the new owner; the transport must report it at
    // once, or the monitor settles the press while its frame is still unknown and the timeline
    // edits from no playhead.
    const ctx = subject();
    const opening = ctx.monitor.session.open();
    ctx.publish(
      {
        status: "opening",
        blocker: null,
        browserPreviewOnly: true,
        durationFrames: 24,
      },
      null as unknown as number,
    );
    expect(ctx.monitor.status().frame).toBeNull();
    const replacement = ctx.monitor.replace(moved(48));
    expect(ctx.monitor.status()).toMatchObject({
      frame: 0,
      rebinding: true,
    });
    await ctx.monitor.session.seek(12);
    expect(ctx.monitor.status().frame).toBe(12);
    expect(ctx.session.seek).not.toHaveBeenCalled();
    // The replacement waits for the first opening before it asks the session to replace, so
    // its own pending call appears only after the opening settles.
    for (let turn = 0; turn < 8; turn += 1) {
      for (const pending of ctx.pending.splice(0)) pending.resolve();
      await Promise.resolve();
    }
    await opening;
    await replacement;
    for (let turn = 0; turn < 4; turn += 1) await Promise.resolve();
    expect(ctx.replacementTarget()).toBe(12);
    expect(ctx.session.seek).not.toHaveBeenCalled();
    // The new owner presents the sought frame.
    ctx.publish(
      {
        status: "paused",
        blocker: null,
        browserPreviewOnly: true,
        durationFrames: 48,
      },
      12,
    );
    expect(ctx.monitor.status().frame).toBe(12);
  });

  it("holds a Play pressed during the rebind and plays the new owner once it is ready", async () => {
    const ctx = subject();
    ctx.publish(undefined, 10);
    const replacement = ctx.monitor.replace(moved(24));
    await ctx.monitor.session.play();
    expect(ctx.session.play).not.toHaveBeenCalled();
    ctx.pending[0]!.resolve();
    await replacement;
    await Promise.resolve();
    await Promise.resolve();
    expect(ctx.replacementTarget()).toBe(10);
    expect(ctx.session.seek).not.toHaveBeenCalled();
    expect(ctx.session.play).toHaveBeenCalledOnce();
  });

  it("B-M2564-09: makes a frame step during the rebind a held seek from the kept frame", async () => {
    // The session's step pauses first. Pausing the owner a rebind is still opening is refused,
    // and the session latched source_unavailable: a `.` straight after a selection broke the
    // monitor. A step is a position change like a ruler press, so it is held the same way.
    const ctx = subject();
    ctx.publish(undefined, 10);
    const replacement = ctx.monitor.replace(moved(24));
    await ctx.monitor.session.step(1);
    expect(ctx.monitor.status().frame).toBe(11);
    await ctx.monitor.session.step(-1);
    await ctx.monitor.session.step(-1);
    expect(ctx.monitor.status().frame).toBe(9);
    expect(ctx.session.step).not.toHaveBeenCalled();
    expect(ctx.session.pause).not.toHaveBeenCalled();
    expect(ctx.session.seek).not.toHaveBeenCalled();
    ctx.pending[0]!.resolve();
    await replacement;
    expect(ctx.replacementTarget()).toBe(9);
    expect(ctx.session.seek).not.toHaveBeenCalled();
    expect(ctx.monitor.status().frame).toBe(9);
  });

  it("B-M2564-09: keeps a step during the rebind inside the new duration", async () => {
    const ctx = subject();
    ctx.publish(undefined, 20);
    const replacement = ctx.monitor.replace(moved(6));
    expect(ctx.monitor.status().frame).toBe(5);
    await ctx.monitor.session.step(1);
    expect(ctx.monitor.status().frame).toBe(5);
    ctx.publish(undefined, 0);
    ctx.pending[0]!.resolve();
    await replacement;
    expect(ctx.session.step).not.toHaveBeenCalled();
    expect(ctx.replacementTarget()).toBe(5);
    expect(ctx.session.seek).not.toHaveBeenCalled();
  });

  it("B-M2564-09: ignores a step during the first opening and passes one on once ready", async () => {
    // During the first opening the transport is unavailable and there is no position to step
    // from, as for Play; stepping the opening owner would latch source_unavailable.
    const ctx = subject();
    const opening = ctx.monitor.session.open();
    await ctx.monitor.session.step(1);
    expect(ctx.session.step).not.toHaveBeenCalled();
    expect(ctx.session.pause).not.toHaveBeenCalled();
    ctx.pending[0]!.resolve();
    await opening;
    await ctx.monitor.session.step(1);
    expect(ctx.session.step).toHaveBeenCalledExactlyOnceWith(1);
  });

  it("drops a held Play when Pause follows it during the rebind", async () => {
    const ctx = subject();
    const replacement = ctx.monitor.replace(moved(24));
    await ctx.monitor.session.play();
    await ctx.monitor.session.pause();
    ctx.pending[0]!.resolve();
    await replacement;
    await Promise.resolve();
    expect(ctx.session.play).not.toHaveBeenCalled();
  });
});

// M25-55: the kept frame is both the logical position and the new owner's first presentation.
// A bootstrap frame-zero publication would make the canvas contradict the cursor during rebind.
describe("NLE monitor rebind publishes what the new owner presents (B-M2564-12)", () => {
  it("notifies when the kept frame is presented and reports each presented frame", async () => {
    const ctx = subject();
    ctx.publish(undefined, 10);
    const shown: (number | null)[] = [];
    ctx.monitor.subscribe(() =>
      shown.push(ctx.session.getPresentation().frame),
    );
    const replacement = ctx.monitor.replace({
      snapshot: { output: { durationFrames: 24 } },
    } as unknown as VisualCompositionBinding);
    const duringRebind = ctx.monitor.status();
    // A stale publication is suppressed while the new owner opens at the kept frame.
    ctx.publish(undefined, 0);
    ctx.pending[0]!.resolve();
    await replacement;
    expect(ctx.replacementTarget()).toBe(10);
    expect(ctx.session.seek).not.toHaveBeenCalled();
    await Promise.resolve();
    expect(shown).not.toContain(0);
    // The monitor's last notification saw the kept frame on the canvas.
    expect(shown.at(-1)).toBe(10);
    // The status names the picture apart from the transport: nothing during the rebind, then
    // the kept frame once the new owner shows it.
    expect(duringRebind).toMatchObject({ frame: 10, presentedFrame: null });
    expect(ctx.monitor.status()).toMatchObject({
      frame: 10,
      presentedFrame: 10,
    });
  });
});
