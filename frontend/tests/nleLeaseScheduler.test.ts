import { describe, expect, it, vi } from "vitest";

import { AuthoringMediaSourceLeaseError } from "../src/host/authoringMediaSourceLease";
import { createNleLeaseScheduler } from "../src/host/nleLeaseScheduler";

const deferred = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((accept, refuse) => {
    resolve = accept;
    reject = refuse;
  });
  return { promise, resolve, reject };
};

describe("M25-48 lease scheduler", () => {
  it("keeps a demanded active route when unrelated decoration keys disappear", async () => {
    const first = deferred<{ value: string; release(): Promise<void> }>();
    const published: string[] = [];
    let activeSignal: AbortSignal | undefined;
    const activeAcquire = vi.fn((signal: AbortSignal) => {
      activeSignal = signal;
      return first.promise;
    });
    const active = { key: "timeline:filmstrip", acquire: activeAcquire };
    const scheduler = createNleLeaseScheduler<string>();
    scheduler.updateDecorationDemand(
      [
        active,
        {
          key: "media:thumbnail-completed-later",
          acquire: async () => ({
            value: "old",
            release: async () => undefined,
          }),
        },
      ],
      (value) => published.push(value),
    );
    await Promise.resolve();
    expect(scheduler.snapshot().activeKey).toBe(active.key);

    scheduler.updateDecorationDemand(
      [
        active,
        {
          key: "timeline:waveform",
          acquire: async () => ({
            value: "waveform",
            release: async () => undefined,
          }),
        },
      ],
      (value) => published.push(value),
    );

    expect(activeSignal?.aborted).toBe(false);
    expect(scheduler.snapshot()).toMatchObject({
      activeKey: active.key,
      pendingKeys: ["timeline:waveform"],
    });
    first.resolve({ value: "filmstrip", release: async () => undefined });
    await scheduler.whenIdle();
    expect(activeAcquire).toHaveBeenCalledOnce();
    expect(published).toEqual(["filmstrip", "waveform"]);
  });

  it("runs one visible decoration at a time and releases before publishing", async () => {
    const first = deferred<{ value: string; release(): Promise<void> }>();
    const order: string[] = [];
    const scheduler = createNleLeaseScheduler<string>();
    scheduler.updateDecorationDemand(
      [
        { key: "first", acquire: () => first.promise },
        {
          key: "second",
          acquire: async () => ({
            value: "two",
            release: async () => {
              order.push("release-two");
            },
          }),
        },
      ],
      (value) => order.push(`publish-${value}`),
    );
    await Promise.resolve();
    expect(scheduler.snapshot().activeKey).toBe("first");
    expect(scheduler.snapshot().pendingKeys).toEqual(["second"]);
    first.resolve({
      value: "one",
      release: async () => {
        order.push("release-one");
      },
    });
    await scheduler.whenIdle();
    expect(order).toEqual([
      "release-one",
      "publish-one",
      "release-two",
      "publish-two",
    ]);
  });

  it("aborts decoration for playback and retries playback resource_limit once", async () => {
    const decorationSignals: AbortSignal[] = [];
    const scheduler = createNleLeaseScheduler<string>();
    scheduler.updateDecorationDemand(
      [
        {
          key: "visible",
          acquire: (signal) => {
            decorationSignals.push(signal);
            return new Promise((_resolve, reject) =>
              signal.addEventListener(
                "abort",
                () =>
                  reject(new AuthoringMediaSourceLeaseError("cancelled", 499)),
                { once: true },
              ),
            );
          },
        },
      ],
      vi.fn(),
    );
    await Promise.resolve();
    let attempts = 0;
    const result = await scheduler.acquirePlayback(async () => {
      attempts += 1;
      if (attempts === 1)
        throw new AuthoringMediaSourceLeaseError("resource_limit", 413);
      return "playback";
    });
    expect(result).toBe("playback");
    expect(attempts).toBe(2);
    expect(decorationSignals[0]?.aborted).toBe(true);
    expect(decorationSignals[1]?.aborted).toBe(false);
    scheduler.updateDecorationDemand([], vi.fn());
    await scheduler.whenIdle();
  });

  it("waits for delayed decoration release before the sole playback retry", async () => {
    const release = deferred<void>();
    let released = false;
    const scheduler = createNleLeaseScheduler<string>();
    scheduler.updateDecorationDemand(
      [
        {
          key: "visible",
          acquire: async () => ({
            value: "thumbnail",
            async release() {
              await release.promise;
              released = true;
            },
          }),
        },
      ],
      vi.fn(),
    );
    await Promise.resolve();

    let attempts = 0;
    const playback = scheduler.acquirePlayback(async () => {
      attempts += 1;
      expect(released).toBe(true);
      return "playback";
    });
    await Promise.resolve();
    expect(attempts).toBe(0);
    release.resolve();
    await expect(playback).resolves.toBe("playback");
    expect(attempts).toBe(1);
    await scheduler.whenIdle();
  });

  it("joins cleanup before first playback and delays its sole busy retry", async () => {
    const release = deferred<void>();
    let released = false;
    const callbacks: (() => void)[] = [];
    const delays: number[] = [];
    const scheduler = createNleLeaseScheduler<string>({
      schedule(callback, delayMs) {
        callbacks.push(callback);
        delays.push(delayMs);
        return callback;
      },
      cancelScheduled: vi.fn(),
    });
    scheduler.updateDecorationDemand(
      [
        {
          key: "visible",
          acquire: async () => ({
            value: "thumbnail",
            async release() {
              await release.promise;
              released = true;
            },
          }),
        },
      ],
      vi.fn(),
    );
    await Promise.resolve();

    let attempts = 0;
    const playback = scheduler.acquirePlayback(async () => {
      attempts += 1;
      expect(released).toBe(true);
      if (attempts === 1) throw new AuthoringMediaSourceLeaseError("busy", 429);
      return "playback";
    });
    const result = playback.catch((error: unknown) => error);
    await Promise.resolve();
    expect(attempts).toBe(0);
    release.resolve();
    await vi.waitFor(() => expect(attempts).toBe(1));
    expect(delays).toEqual([1_000]);
    callbacks.shift()?.();
    await expect(result).resolves.toBe("playback");
    expect(attempts).toBe(2);
    await scheduler.whenIdle();
  });

  it("delays the sole busy playback retry until the server cleanup window settles", async () => {
    const callbacks: (() => void)[] = [];
    const delays: number[] = [];
    const scheduler = createNleLeaseScheduler<string>({
      schedule(callback, delayMs) {
        callbacks.push(callback);
        delays.push(delayMs);
        return callback;
      },
      cancelScheduled: vi.fn(),
    });
    let attempts = 0;
    const playback = scheduler.acquirePlayback(async () => {
      attempts += 1;
      if (attempts === 1) throw new AuthoringMediaSourceLeaseError("busy", 429);
      return "playback";
    });

    await Promise.resolve();
    expect(attempts).toBe(1);
    expect(delays).toEqual([1_000]);
    callbacks.shift()?.();
    await expect(playback).resolves.toBe("playback");
    expect(attempts).toBe(2);
    await scheduler.whenIdle();
  });

  it("cancels a pending busy retry when the scheduler closes", async () => {
    const handle = Symbol("busy-retry");
    const cancelScheduled = vi.fn();
    const scheduler = createNleLeaseScheduler<string>({
      schedule: () => handle,
      cancelScheduled,
    });
    let attempts = 0;
    const playback = scheduler.acquirePlayback(async () => {
      attempts += 1;
      throw new AuthoringMediaSourceLeaseError("busy", 429);
    });
    const rejection = expect(playback).rejects.toMatchObject({
      disposition: "busy",
    });

    await Promise.resolve();
    scheduler.close();
    await rejection;
    expect(attempts).toBe(1);
    expect(cancelScheduled).toHaveBeenCalledOnce();
    expect(cancelScheduled).toHaveBeenCalledWith(handle);
  });

  it("surfaces decoration cleanup failure before starting playback", async () => {
    const cleanupFailure = new Error("cleanup failed");
    const release = deferred<void>();
    const scheduler = createNleLeaseScheduler<string>();
    scheduler.updateDecorationDemand(
      [
        {
          key: "visible",
          acquire: async () => ({
            value: "thumbnail",
            release: () => release.promise,
          }),
        },
      ],
      vi.fn(),
    );
    await Promise.resolve();
    let attempts = 0;
    const playback = scheduler.acquirePlayback(async () => {
      attempts += 1;
      return "unreachable";
    });
    const rejection = expect(playback).rejects.toBe(cleanupFailure);
    release.reject(cleanupFailure);
    await rejection;
    expect(attempts).toBe(0);
    await scheduler.whenIdle();
  });

  it("bounds a playback retry when aborted decoration work never settles", async () => {
    vi.useFakeTimers();
    try {
      const scheduler = createNleLeaseScheduler<string>();
      scheduler.updateDecorationDemand(
        [
          {
            key: "stuck",
            acquire: () => new Promise(() => undefined),
          },
        ],
        vi.fn(),
      );
      await Promise.resolve();
      const operation = vi.fn(async () => "unreachable");
      const playback = scheduler.acquirePlayback(operation);
      const rejection = expect(playback).rejects.toMatchObject({
        name: "TimeoutError",
      });
      await vi.advanceTimersByTimeAsync(5_000);
      await rejection;
      expect(operation).not.toHaveBeenCalled();
      scheduler.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("discards an aborted decode before reacquiring its still-visible demand", async () => {
    const first = deferred<{
      value: string;
      release(): Promise<void>;
      discard(): void;
    }>();
    const discarded = vi.fn();
    const published: string[] = [];
    let attempts = 0;
    const scheduler = createNleLeaseScheduler<string>();
    scheduler.updateDecorationDemand(
      [
        {
          key: "visible",
          acquire: () => {
            attempts += 1;
            return attempts === 1
              ? first.promise
              : Promise.resolve({ value: "current", release: async () => {} });
          },
        },
      ],
      (value) => published.push(value),
    );
    await Promise.resolve();
    const playback = scheduler.acquirePlayback(async () => "playback");
    first.resolve({
      value: "stale",
      release: async () => {},
      discard: discarded,
    });
    await playback;
    await scheduler.whenIdle();
    expect(discarded).toHaveBeenCalledTimes(1);
    expect(published).toEqual(["current"]);
    expect(attempts).toBe(2);
  });

  it.each([
    ["resource_limit", 413],
    ["busy", 429],
  ] as const)(
    "backs decoration %s refusals off at 1, 2 and 4 seconds then idles",
    async (disposition, status) => {
      const delays: number[] = [];
      const callbacks: (() => void)[] = [];
      let attempts = 0;
      const scheduler = createNleLeaseScheduler<string>({
        schedule(callback, delayMs) {
          delays.push(delayMs);
          callbacks.push(callback);
          return callback;
        },
        cancelScheduled: vi.fn(),
      });
      scheduler.updateDecorationDemand(
        [
          {
            key: "visible",
            acquire: async () => {
              attempts += 1;
              throw new AuthoringMediaSourceLeaseError(disposition, status);
            },
          },
        ],
        vi.fn(),
      );
      for (let index = 0; index < 3; index += 1) {
        await Promise.resolve();
        callbacks.shift()?.();
      }
      await scheduler.whenIdle();
      expect(delays).toEqual([1_000, 2_000, 4_000]);
      expect(attempts).toBe(4);
      expect(scheduler.snapshot().idleAfterBackoff).toBe(true);
      scheduler.updateDecorationDemand([], vi.fn());
      expect(scheduler.snapshot().pendingKeys).toEqual([]);
    },
  );

  it("keeps a bounded busy retry when unrelated decoration keys change", async () => {
    const callbacks: (() => void)[] = [];
    const delays: number[] = [];
    const cancelled: unknown[] = [];
    let attempts = 0;
    const busyThenReady = {
      key: "timeline:filmstrip",
      async acquire() {
        attempts += 1;
        if (attempts === 1)
          throw new AuthoringMediaSourceLeaseError("busy", 429);
        return { value: "filmstrip", release: async () => undefined };
      },
    };
    const scheduler = createNleLeaseScheduler<string>({
      schedule(callback, delayMs) {
        callbacks.push(callback);
        delays.push(delayMs);
        return callback;
      },
      cancelScheduled(handle) {
        cancelled.push(handle);
      },
    });
    scheduler.updateDecorationDemand(
      [
        busyThenReady,
        {
          key: "media:thumbnail-one",
          acquire: async () => ({
            value: "old",
            release: async () => undefined,
          }),
        },
      ],
      vi.fn(),
    );
    await vi.waitFor(() => expect(delays).toEqual([1_000]));

    scheduler.updateDecorationDemand(
      [
        busyThenReady,
        {
          key: "timeline:waveform",
          acquire: async () => ({
            value: "waveform",
            release: async () => undefined,
          }),
        },
      ],
      vi.fn(),
    );

    expect(attempts).toBe(1);
    expect(delays).toEqual([1_000]);
    expect(cancelled).toEqual([]);
    callbacks.shift()?.();
    await scheduler.whenIdle();
    expect(attempts).toBe(2);
    expect(scheduler.snapshot().idleAfterBackoff).toBe(false);
  });

  it("close aborts work and timers without publishing", async () => {
    const pending = deferred<{ value: string; release(): Promise<void> }>();
    const publish = vi.fn();
    const scheduler = createNleLeaseScheduler<string>();
    scheduler.updateDecorationDemand(
      [{ key: "visible", acquire: () => pending.promise }],
      publish,
    );
    await Promise.resolve();
    scheduler.close();
    pending.reject(new AuthoringMediaSourceLeaseError("cancelled", 499));
    await scheduler.whenIdle();
    expect(publish).not.toHaveBeenCalled();
    expect(scheduler.snapshot().closed).toBe(true);
  });
});
