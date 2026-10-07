import { describe, expect, it, vi } from "vitest";

import type { DurationResolution } from "../src/host/durationResolutionClient";
import { createDurationResolutionController } from "../src/state/durationResolutionState";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, refuse) => {
    resolve = accept;
    reject = refuse;
  });
  return { promise, resolve, reject };
}

function resolution(seconds: number, frames: number): DurationResolution {
  return {
    schema: "h3.context.duration_resolution.v1",
    requested_seconds: seconds,
    requested_milliseconds: seconds * 1000,
    effective_milliseconds: seconds * 1000,
    frame_count: frames,
    snapped: false,
  };
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe("duration resolution state", () => {
  it("publishes resolving and the exact current resolution", async () => {
    const pending = deferred<DurationResolution>();
    const changed = vi.fn();
    const controller = createDurationResolutionController({
      resolve: async () => pending.promise,
      changed,
    });

    controller.request(8);
    expect(controller.snapshot()).toEqual({
      status: "resolving",
      requestedSeconds: 8,
    });
    pending.resolve(resolution(8, 192));
    await settle();
    expect(controller.snapshot()).toEqual({
      status: "resolved",
      resolution: resolution(8, 192),
    });
    expect(changed).toHaveBeenCalledTimes(2);
  });

  it("aborts and ignores an older transport that settles late", async () => {
    const old = deferred<DurationResolution>();
    const current = deferred<DurationResolution>();
    const signals: AbortSignal[] = [];
    const controller = createDurationResolutionController({
      resolve: (seconds, signal) => {
        signals.push(signal);
        return seconds === 7 ? old.promise : current.promise;
      },
      changed: vi.fn(),
    });

    controller.request(7);
    controller.request(8);
    expect(signals[0]?.aborted).toBe(true);
    current.resolve(resolution(8, 192));
    await settle();
    old.resolve(resolution(7, 175));
    await settle();
    expect(controller.snapshot()).toEqual({
      status: "resolved",
      resolution: resolution(8, 192),
    });
  });

  it("publishes a content-free refusal and retries the same integer", async () => {
    const retry = deferred<DurationResolution>();
    let attempts = 0;
    const controller = createDurationResolutionController({
      resolve: async () => {
        attempts += 1;
        if (attempts === 1) throw new Error("private host detail");
        return retry.promise;
      },
      changed: vi.fn(),
    });

    controller.request(8);
    await settle();
    expect(controller.snapshot()).toEqual({
      status: "refused",
      requestedSeconds: 8,
    });
    controller.retry();
    expect(controller.snapshot()).toEqual({
      status: "resolving",
      requestedSeconds: 8,
    });
    retry.resolve(resolution(8, 192));
    await settle();
    expect(controller.snapshot()?.status).toBe("resolved");
  });
});
