export type NleDecorationLease<T> = Readonly<{
  value: T;
  release(): Promise<void>;
  discard?(): void;
}>;

export type NleDecorationDemand<T> = Readonly<{
  key: string;
  /** Content-free category used by scheduler diagnostics. */
  kind?: string;
  acquire(signal: AbortSignal): Promise<NleDecorationLease<T>>;
}>;

export type NleLeaseSchedulerEvent = Readonly<{
  sequence: number;
  epochMs: number;
  monotonicMs: number;
  event: string;
  playbackDepth: number;
  activeKind: string | null;
  pendingKinds: readonly (string | null)[];
  retryIndex: number;
  retryScheduled: boolean;
  idleAfterBackoff: boolean;
  reason: string | null;
}>;

export type NleLeaseSchedulerDependencies = Readonly<{
  schedule(callback: () => void, delayMs: number): unknown;
  cancelScheduled(handle: unknown): void;
  onDiagnostic?(event: NleLeaseSchedulerEvent): void;
}>;

export type NlePlaybackLeaseScheduler = Readonly<{
  acquirePlayback<R>(operation: () => Promise<R>): Promise<R>;
}>;

export type NleLeaseScheduler<T> = NlePlaybackLeaseScheduler &
  Readonly<{
    updateDecorationDemand(
      demands: readonly NleDecorationDemand<T>[],
      publish: (value: T) => void,
    ): void;
    snapshot(): Readonly<{
      activeKey: string | null;
      pendingKeys: readonly string[];
      idleAfterBackoff: boolean;
      closed: boolean;
    }>;
    whenIdle(): Promise<void>;
    close(): void;
  }>;

type ActiveDecoration<T> = {
  demand: NleDecorationDemand<T>;
  controller: AbortController;
  generation: number;
  preemptedForPlayback: boolean;
  cleanupError: unknown | null;
  settled: Promise<void>;
  resolveSettled(): void;
};

const DEFAULT_DEPENDENCIES: NleLeaseSchedulerDependencies = Object.freeze({
  schedule: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  cancelScheduled: (handle) => globalThis.clearTimeout(handle as number),
});

const BACKOFF_MS = Object.freeze([1_000, 2_000, 4_000] as const);
const PLAYBACK_PREEMPTION_WAIT_MS = 5_000;
const PLAYBACK_BUSY_RETRY_DELAY_MS = 1_000;

const dispositionOf = (error: unknown): string | null =>
  typeof error === "object" && error !== null && "disposition" in error
    ? String((error as { disposition: unknown }).disposition)
    : null;

const isTransientCapacityRefusal = (error: unknown): boolean => {
  const disposition = dispositionOf(error);
  // IMPORTANT: the supplied single-worker route reports an occupied derivative build as `busy`
  // (HTTP 429), while the lease pool reports `resource_limit`. Both require the same bounded
  // cleanup/backoff path or decoration churn can permanently starve playback during revisions.
  return disposition === "resource_limit" || disposition === "busy";
};

export const createNleLeaseScheduler = <T>(
  dependencies: Partial<NleLeaseSchedulerDependencies> = {},
): NleLeaseScheduler<T> => {
  const schedulerDependencies = {
    ...DEFAULT_DEPENDENCIES,
    ...dependencies,
  };
  let queue: NleDecorationDemand<T>[] = [];
  let publish: (value: T) => void = () => undefined;
  let active: ActiveDecoration<T> | null = null;
  let scheduled: unknown | null = null;
  let generation = 0;
  let retryIndex = 0;
  let playbackDepth = 0;
  let idleAfterBackoff = false;
  let closed = false;
  const idleWaiters = new Set<() => void>();
  const cancelPlaybackRetryWaits = new Set<() => void>();
  let eventSequence = 0;

  const emit = (event: string, reason: string | null = null): void => {
    if (schedulerDependencies.onDiagnostic === undefined) return;
    try {
      schedulerDependencies.onDiagnostic(
        Object.freeze({
          sequence: ++eventSequence,
          epochMs: Date.now(),
          monotonicMs: globalThis.performance?.now() ?? 0,
          event,
          playbackDepth,
          activeKind: active?.demand.kind ?? null,
          pendingKinds: Object.freeze(queue.map(({ kind }) => kind ?? null)),
          retryIndex,
          retryScheduled: scheduled !== null,
          idleAfterBackoff,
          reason,
        }),
      );
    } catch {
      // IMPORTANT: diagnostic observers must never change lease acquisition or cleanup behavior.
    }
  };

  const isIdle = (): boolean =>
    active === null &&
    scheduled === null &&
    (queue.length === 0 || idleAfterBackoff || playbackDepth > 0 || closed);

  const notifyIdle = (): void => {
    if (!isIdle()) return;
    for (const resolve of idleWaiters) resolve();
    idleWaiters.clear();
  };

  const cancelTimer = (): void => {
    if (scheduled === null) return;
    schedulerDependencies.cancelScheduled(scheduled);
    scheduled = null;
  };

  let pump: () => void;

  const run = async (work: ActiveDecoration<T>): Promise<void> => {
    try {
      emit("decoration.acquire.start");
      const lease = await work.demand.acquire(work.controller.signal);
      emit("decoration.acquire.succeeded");
      try {
        await lease.release();
        emit("decoration.release.succeeded");
      } catch (error) {
        work.cleanupError = error;
        lease.discard?.();
        emit("decoration.release.failed", dispositionOf(error));
        throw error;
      }
      if (
        !closed &&
        !work.controller.signal.aborted &&
        work.generation === generation
      )
        publish(lease.value);
      else lease.discard?.();
      retryIndex = 0;
      idleAfterBackoff = false;
      emit("decoration.publish");
      if (
        work.preemptedForPlayback &&
        !closed &&
        work.generation === generation
      )
        queue.unshift(work.demand);
    } catch (error) {
      emit("decoration.acquire.failed", dispositionOf(error));
      if (work.preemptedForPlayback && error instanceof AggregateError) {
        // IMPORTANT: an aggregate acquisition failure can include failed authority cleanup.
        // Do not retry playback or requeue decoration as though preemption had released it.
        work.cleanupError = error;
      }
      if (
        work.preemptedForPlayback &&
        work.cleanupError === null &&
        !closed &&
        work.generation === generation
      ) {
        queue.unshift(work.demand);
      } else if (
        !closed &&
        work.generation === generation &&
        isTransientCapacityRefusal(error)
      ) {
        if (retryIndex < BACKOFF_MS.length) {
          const delayMs = BACKOFF_MS[retryIndex];
          retryIndex += 1;
          queue.unshift(work.demand);
          scheduled = schedulerDependencies.schedule(() => {
            scheduled = null;
            emit("decoration.retry.ready");
            pump();
          }, delayMs);
          emit("decoration.retry.scheduled", dispositionOf(error));
        } else {
          queue.unshift(work.demand);
          idleAfterBackoff = true;
          emit("decoration.retry.exhausted", dispositionOf(error));
        }
      }
    } finally {
      if (active === work) active = null;
      emit("decoration.settled");
      work.resolveSettled();
      if (scheduled === null) pump();
      notifyIdle();
    }
  };

  pump = (): void => {
    if (
      closed ||
      playbackDepth > 0 ||
      active !== null ||
      scheduled !== null ||
      idleAfterBackoff
    ) {
      notifyIdle();
      return;
    }
    const demand = queue.shift();
    if (!demand) {
      notifyIdle();
      return;
    }
    let resolveSettled!: () => void;
    const settled = new Promise<void>((resolve) => {
      resolveSettled = resolve;
    });
    const work: ActiveDecoration<T> = {
      demand,
      controller: new AbortController(),
      generation,
      preemptedForPlayback: false,
      cleanupError: null,
      settled,
      resolveSettled,
    };
    active = work;
    emit("decoration.scheduled");
    void run(work);
  };

  const preemptDecoration = (reason: string): ActiveDecoration<T> | null => {
    cancelTimer();
    if (!active) return null;
    // IMPORTANT: playback and decoration share one backend pool. Abort the decoration
    // before retrying playback or repeated resource_limit failures can starve playback.
    active.preemptedForPlayback = true;
    emit("decoration.abort", reason);
    active.controller.abort(reason);
    return active;
  };

  const waitForPreemption = (work: ActiveDecoration<T>): Promise<void> =>
    new Promise((resolve, reject) => {
      let finished = false;
      const timeout = globalThis.setTimeout(() => {
        if (finished) return;
        finished = true;
        const error = new Error("decoration cleanup timed out");
        error.name = "TimeoutError";
        reject(error);
      }, PLAYBACK_PREEMPTION_WAIT_MS);
      void work.settled.then(() => {
        if (finished) return;
        finished = true;
        globalThis.clearTimeout(timeout);
        if (work.cleanupError !== null) reject(work.cleanupError);
        else resolve();
      });
    });

  const waitForBusyRetry = (): Promise<boolean> =>
    new Promise((resolve) => {
      let settled = false;
      let handle: unknown;
      const finish = (ready: boolean) => {
        if (settled) return;
        settled = true;
        cancelPlaybackRetryWaits.delete(cancel);
        resolve(ready);
      };
      const cancel = () => {
        schedulerDependencies.cancelScheduled(handle);
        finish(false);
      };
      cancelPlaybackRetryWaits.add(cancel);
      handle = schedulerDependencies.schedule(
        () => finish(true),
        PLAYBACK_BUSY_RETRY_DELAY_MS,
      );
    });

  return Object.freeze({
    updateDecorationDemand(
      demands: readonly NleDecorationDemand<T>[],
      nextPublish: (value: T) => void,
    ): void {
      publish = nextPublish;
      const byKey = new Map(demands.map((demand) => [demand.key, demand]));
      const keepFirst = (first: NleDecorationDemand<T>) => [
        first,
        ...demands.filter((demand) => demand.key !== first.key),
      ];

      if (active !== null && byKey.get(active.demand.key) === active.demand) {
        // The broker keeps routed demand objects stable by source/key. A thumbnail publishing
        // removes that thumbnail from the list but must not abort a filmstrip already in flight.
        queue = demands.filter((demand) => demand.key !== active!.demand.key);
        emit("demand.update");
        return;
      }

      const delayedRetry = scheduled === null ? undefined : queue[0];
      if (
        delayedRetry !== undefined &&
        byKey.get(delayedRetry.key) === delayedRetry
      ) {
        queue = keepFirst(delayedRetry);
        emit("demand.update");
        return;
      }

      const exhaustedRetry = idleAfterBackoff ? queue[0] : undefined;
      if (
        exhaustedRetry !== undefined &&
        byKey.get(exhaustedRetry.key) === exhaustedRetry
      ) {
        queue = keepFirst(exhaustedRetry);
        emit("demand.update");
        return;
      }

      generation += 1;
      retryIndex = 0;
      idleAfterBackoff = false;
      cancelTimer();
      if (active !== null) {
        const reason = byKey.has(active.demand.key)
          ? "demand_updated"
          : "demand_removed";
        emit("decoration.abort", reason);
        active.controller.abort(reason);
      }
      queue = [...demands];
      emit("demand.update");
      pump();
    },

    async acquirePlayback<R>(operation: () => Promise<R>): Promise<R> {
      playbackDepth += 1;
      emit("playback.acquire.start");
      const pendingCleanup = preemptDecoration("playback_priority");
      try {
        // IMPORTANT: abort only requests decoration cancellation. Join its release before the
        // first playback acquisition or a minted lease can keep the serial backend claim busy and
        // turn an ordinary Media-bin insertion into terminal source_unavailable.
        if (pendingCleanup !== null) await waitForPreemption(pendingCleanup);
        try {
          const result = await operation();
          emit("playback.acquire.succeeded");
          return result;
        } catch (error) {
          if (!isTransientCapacityRefusal(error)) throw error;
          const cleanup = preemptDecoration("playback_retry");
          if (cleanup !== null) {
            try {
              // IMPORTANT: abort is only a signal. Retry after bounded cleanup joins,
              // or both playback attempts can race the same decoration lease.
              await waitForPreemption(cleanup);
            } catch (cleanupError) {
              throw new AggregateError(
                [error, cleanupError],
                "playback retry blocked by decoration cleanup",
              );
            }
          }
          // IMPORTANT: abort completion only joins the browser owner; the supplied route may
          // retain its single derivative worker briefly while server cleanup finishes. Delay the
          // sole `busy` retry or it can hit the same 429 and permanently blank the monitor.
          if (dispositionOf(error) === "busy" && !(await waitForBusyRetry()))
            throw error;
          const result = await operation();
          emit("playback.acquire.succeeded");
          return result;
        }
      } catch (error) {
        emit("playback.acquire.failed", dispositionOf(error));
        throw error;
      } finally {
        // Playback priority protects acquisition and its bounded cleanup/retry only. The returned
        // visual owner may remain retained for presentation, but it must not starve decorations.
        playbackDepth = Math.max(0, playbackDepth - 1);
        emit("playback.acquire.settled");
        pump();
      }
    },

    snapshot() {
      return Object.freeze({
        activeKey: active?.demand.key ?? null,
        pendingKeys: queue.map(({ key }) => key),
        idleAfterBackoff,
        closed,
      });
    },

    whenIdle(): Promise<void> {
      if (isIdle()) return Promise.resolve();
      return new Promise((resolve) => idleWaiters.add(resolve));
    },

    close(): void {
      if (closed) return;
      closed = true;
      generation += 1;
      cancelTimer();
      for (const cancel of [...cancelPlaybackRetryWaits]) cancel();
      queue = [];
      emit("scheduler.close");
      active?.controller.abort("scheduler_closed");
      notifyIdle();
    },
  });
};
