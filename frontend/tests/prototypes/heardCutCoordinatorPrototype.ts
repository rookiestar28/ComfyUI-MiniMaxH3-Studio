export type HeardClockPair = Readonly<{
  contextTime: number;
  performanceTime: number;
}>;

export type HeardCutDependencies = Readonly<{
  now(): number;
  read(): HeardClockPair;
  release(): void;
  refuse(reason: string): void;
  setTimer(callback: () => void, delayMs: number): number;
  clearTimer(id: number): void;
}>;

export type HeardCutCoordinator = Readonly<{
  arm(cutContextTime: number): void;
  stop(): void;
  snapshot(): Readonly<{
    status: string;
    reason: string | null;
    released: boolean;
    releaseContextTime: number | null;
    refusedAtPerformanceTime: number | null;
    timerActive: boolean;
  }>;
}>;

declare global {
  interface Window {
    __heardCutCoordinatorFactory?: (
      dependencies: HeardCutDependencies,
    ) => HeardCutCoordinator;
  }
}

/** Serializable installer; only the owned qualification page receives this factory. */
export function installHeardCutCoordinatorPrototype() {
  const create = (dependencies: HeardCutDependencies): HeardCutCoordinator => {
    let status = "idle";
    let reason: string | null = null;
    let stopped = false;
    let released = false;
    let releaseContextTime: number | null = null;
    let refusedAtPerformanceTime: number | null = null;
    let timer: number | null = null;
    let deadline = 0;
    let previous: HeardClockPair | null = null;
    let lastAdvance = 0;
    const cancel = () => {
      if (timer !== null) dependencies.clearTimer(timer);
      timer = null;
    };
    const refuse = (value: string) => {
      if (stopped) return;
      stopped = true;
      status = "refused";
      reason = value;
      refusedAtPerformanceTime = dependencies.now();
      cancel();
      dependencies.refuse(value);
    };
    const tick = () => {
      timer = null;
      if (stopped) return;
      const now = dependencies.now();
      const pair = dependencies.read();
      // CRITICAL: interpolated wall time cannot prove raw output-clock progress.
      // Never substitute rendered currentTime; a frozen heard pair must refuse.
      if (
        !Number.isFinite(now) ||
        !Number.isFinite(pair.contextTime) ||
        !Number.isFinite(pair.performanceTime) ||
        pair.contextTime <= 0 ||
        pair.performanceTime <= 0
      ) {
        refuse("output_clock_invalid");
        return;
      }
      if (Math.abs(now - pair.performanceTime) > 100) {
        // An unchanged pair can expire before the polling progress budget. Preserve
        // its stalled cause instead of classifying the same frozen clock as corrupt.
        refuse(
          previous !== null &&
            pair.contextTime === previous.contextTime &&
            pair.performanceTime === previous.performanceTime
            ? "output_clock_stalled"
            : "output_clock_invalid",
        );
        return;
      }
      if (
        previous !== null &&
        (pair.contextTime < previous.contextTime ||
          pair.performanceTime < previous.performanceTime)
      ) {
        refuse("output_clock_regressed");
        return;
      }
      if (
        previous === null ||
        (pair.contextTime > previous.contextTime &&
          pair.performanceTime > previous.performanceTime)
      )
        lastAdvance = now;
      else if (now - lastAdvance >= 100) {
        refuse("output_clock_stalled");
        return;
      }
      previous = { ...pair };
      const heard = pair.contextTime + (now - pair.performanceTime) / 1000;
      if (!released && heard >= deadline) {
        refuse("cut_deadline_missed");
        return;
      }
      // IMPORTANT: native startup was confirmed before arm. Primary video callbacks
      // cannot own this deadline; a late callback otherwise starts video after PCM.
      if (!released && heard >= deadline - 0.01) {
        released = true;
        status = "released";
        releaseContextTime = heard;
        dependencies.release();
      }
      if (!stopped) timer = dependencies.setTimer(tick, 4);
    };
    return {
      arm(cutContextTime) {
        if (status !== "idle" || stopped)
          throw new Error("heard_cut_owner_already_armed_or_stopped");
        if (!Number.isFinite(cutContextTime) || cutContextTime <= 0)
          throw new Error("heard_cut_deadline_invalid");
        deadline = cutContextTime;
        status = "watching";
        tick();
      },
      stop() {
        if (stopped) return;
        stopped = true;
        status = "stopped";
        cancel();
      },
      snapshot() {
        return {
          status,
          reason,
          released,
          releaseContextTime,
          refusedAtPerformanceTime,
          timerActive: timer !== null,
        };
      },
    };
  };
  window.__heardCutCoordinatorFactory = create;
  return create;
}
