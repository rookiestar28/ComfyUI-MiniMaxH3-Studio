import { afterEach, describe, expect, it, vi } from "vitest";

import {
  installHeardCutCoordinatorPrototype,
  type HeardClockPair,
} from "./prototypes/heardCutCoordinatorPrototype";

afterEach(() => {
  delete window.__heardCutCoordinatorFactory;
});

function fixture() {
  let now = 100;
  let pair: HeardClockPair = { contextTime: 1, performanceTime: 100 };
  let timer: (() => void) | null = null;
  const release = vi.fn();
  const refuse = vi.fn();
  const clearTimer = vi.fn(() => {
    timer = null;
  });
  const coordinator = installHeardCutCoordinatorPrototype()({
    now: () => now,
    read: () => pair,
    release,
    refuse,
    setTimer: (callback, delay) => {
      expect(delay).toBe(4);
      timer = callback;
      return 1;
    },
    clearTimer,
  });
  return {
    coordinator,
    release,
    refuse,
    clearTimer,
    scheduled: () => timer,
    sample(nextNow: number, nextPair: HeardClockPair) {
      now = nextNow;
      pair = nextPair;
      const callback = timer;
      timer = null;
      callback?.();
    },
  };
}

describe("heard cut deadline and clock ownership", () => {
  it("releases once from heard time without any primary-video callback", () => {
    const run = fixture();
    run.coordinator.arm(1.5);
    run.sample(580, { contextTime: 1.48, performanceTime: 580 });
    expect(run.release).not.toHaveBeenCalled();
    run.sample(592, { contextTime: 1.492, performanceTime: 592 });
    expect(run.release).toHaveBeenCalledTimes(1);
    run.sample(620, { contextTime: 1.52, performanceTime: 620 });
    expect(run.release).toHaveBeenCalledTimes(1);
    expect(run.coordinator.snapshot()).toMatchObject({
      status: "released",
      releaseContextTime: 1.492,
      reason: null,
    });
    run.coordinator.stop();
  });

  it("refuses a late timer before it can release the native owner", () => {
    const run = fixture();
    run.coordinator.arm(1.5);
    run.sample(680, { contextTime: 1.58, performanceTime: 680 });
    expect(run.release).not.toHaveBeenCalled();
    expect(run.refuse).toHaveBeenCalledExactlyOnceWith("cut_deadline_missed");
    expect(run.coordinator.snapshot().timerActive).toBe(false);
  });

  it.each([
    { contextTime: 0, performanceTime: 0 },
    { contextTime: Number.NaN, performanceTime: 100 },
    { contextTime: 1, performanceTime: -1 },
    { contextTime: 1, performanceTime: 201 },
  ])(
    "rejects an invalid raw pair rather than using rendered time: %j",
    (pair) => {
      const run = fixture();
      run.coordinator.arm(1.5);
      run.sample(100, pair);
      expect(run.refuse).toHaveBeenCalledExactlyOnceWith(
        "output_clock_invalid",
      );
      expect(run.release).not.toHaveBeenCalled();
    },
  );

  it("refuses a frozen raw pair although wall-time interpolation still moves", () => {
    const run = fixture();
    run.coordinator.arm(1.5);
    run.sample(196, { contextTime: 1, performanceTime: 100 });
    expect(run.refuse).not.toHaveBeenCalled();
    run.sample(200, { contextTime: 1, performanceTime: 100 });
    expect(run.refuse).toHaveBeenCalledExactlyOnceWith("output_clock_stalled");
    expect(run.coordinator.snapshot().refusedAtPerformanceTime).toBe(200);
    expect(run.release).not.toHaveBeenCalled();
  });

  it("rejects clock regression and cannot arm a refused owner again", () => {
    const run = fixture();
    run.coordinator.arm(1.5);
    run.sample(104, { contextTime: 0.99, performanceTime: 104 });
    expect(run.refuse).toHaveBeenCalledExactlyOnceWith(
      "output_clock_regressed",
    );
    expect(() => run.coordinator.arm(1.6)).toThrow("already_armed_or_stopped");
  });

  it("classifies an unchanged pair that ages past mapping validity as stalled", () => {
    const run = fixture();
    run.coordinator.arm(1.5);
    run.sample(204, { contextTime: 1, performanceTime: 100 });
    expect(run.refuse).toHaveBeenCalledExactlyOnceWith("output_clock_stalled");
    expect(run.release).not.toHaveBeenCalled();
  });

  it("stop fences even an already-delivered timer and is idempotent", () => {
    const run = fixture();
    run.coordinator.arm(1.5);
    const delivered = run.scheduled();
    run.coordinator.stop();
    run.coordinator.stop();
    delivered?.();
    expect(run.clearTimer).toHaveBeenCalledTimes(1);
    expect(run.release).not.toHaveBeenCalled();
    expect(run.refuse).not.toHaveBeenCalled();
    expect(run.coordinator.snapshot().timerActive).toBe(false);
  });
});
