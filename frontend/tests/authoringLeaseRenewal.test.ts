import { describe, expect, it, vi } from "vitest";
import { createAuthoringLeaseRenewal } from "../src/host/authoringLeaseRenewal";
import { AuthoringMediaSourceLeaseError } from "../src/host/authoringMediaLeaseTransport";

function clock(ttl = 60_000) {
  const jobs: { callback(): void; delay: number; live: boolean }[] = [];
  let eligible = true;
  const renew = vi.fn<(signal: AbortSignal) => Promise<void>>(
    async () => undefined,
  );
  const failure = vi.fn();
  const cancel = vi.fn((handle: unknown) => {
    (handle as (typeof jobs)[number]).live = false;
  });
  const schedule = vi.fn((callback: () => void, delay: number) => {
    const job = { callback, delay, live: true };
    jobs.push(job);
    return job;
  });
  const control = createAuthoringLeaseRenewal({
    eligible: () => eligible,
    ttlMs: () => ttl,
    renew,
    failure,
    cancel,
    schedule,
  });
  const live = () => jobs.filter((job) => job.live);
  const fire = (delay: number) => {
    const job = live().find((job) => job.delay === delay);
    if (!job) throw new Error(`no live timer at ${delay}`);
    job.live = false;
    job.callback();
    return job;
  };
  return {
    control,
    renew,
    failure,
    cancel,
    schedule,
    jobs,
    live,
    fire,
    eligible(value: boolean) {
      eligible = value;
    },
  };
}

const settle = async () => {
  await Promise.resolve();
  await Promise.resolve();
};

describe("renewal control through its independent clock and renewal seams", () => {
  it.each([
    [60_000, 30_000, 30_000],
    [601, 300, 301],
    [1, 1, 1],
  ])(
    "pins the half-life and original deadline for ttl %i",
    async (ttl, half, rest) => {
      const h = clock(ttl);
      h.control.start();
      expect(h.live().map((job) => job.delay)).toEqual([half]);
      h.fire(half);
      await settle();
      expect(h.renew).toHaveBeenCalledOnce();
      expect(h.renew.mock.calls[0]![0].aborted).toBe(false);
      expect(h.live().map((job) => job.delay)).toEqual([rest]);
      h.control.stop();
      expect(h.live()).toEqual([]);
      expect(h.renew.mock.calls[0]![0].aborted).toBe(true);
      expect(h.failure).not.toHaveBeenCalled();
    },
  );

  it("retries busy twice on the same signal and deadline, then starts a new lifetime without aborting the completed renewal", async () => {
    const h = clock();
    h.renew
      .mockRejectedValueOnce(new AuthoringMediaSourceLeaseError("busy", 429))
      .mockRejectedValueOnce(new AuthoringMediaSourceLeaseError("busy", 429))
      .mockImplementationOnce(async (signal) => h.control.start(signal));
    h.control.start();
    h.fire(30_000);
    const originalDeadline = h.live()[0]!;
    await settle();
    expect(h.live().map((job) => job.delay)).toEqual([30_000, 250]);
    h.fire(250);
    await settle();
    expect(h.live()[0]).toBe(originalDeadline);
    expect(h.live().map((job) => job.delay)).toEqual([30_000, 250]);
    h.fire(250);
    await settle();
    expect(h.renew).toHaveBeenCalledTimes(3);
    expect(new Set(h.renew.mock.calls.map(([signal]) => signal)).size).toBe(1);
    expect(h.renew.mock.calls[0]![0].aborted).toBe(false);
    expect(originalDeadline.live).toBe(false);
    expect(h.live()).toHaveLength(1);
    expect(h.live()[0]).not.toBe(originalDeadline);
    expect(h.live()[0]!.delay).toBe(30_000);
    expect(h.failure).not.toHaveBeenCalled();
    h.control.stop();
  });

  it("does not schedule or renew an ineligible owner", () => {
    const h = clock();
    h.eligible(false);
    h.control.start();
    expect(h.schedule).not.toHaveBeenCalled();
    expect(h.renew).not.toHaveBeenCalled();
    expect(h.failure).not.toHaveBeenCalled();
  });

  it("rechecks eligibility independently at the attempt and deadline", async () => {
    const h = clock();
    h.control.start();
    h.eligible(false);
    h.fire(30_000);
    await settle();
    expect(h.renew).not.toHaveBeenCalled();
    h.fire(30_000);
    expect(h.failure).not.toHaveBeenCalled();
    h.control.stop();
  });

  it("rechecks eligibility before a busy retry", async () => {
    const h = clock();
    h.renew.mockRejectedValueOnce(
      new AuthoringMediaSourceLeaseError("busy", 429),
    );
    h.control.start();
    h.fire(30_000);
    await settle();
    h.eligible(false);
    h.fire(250);
    await settle();
    expect(h.renew).toHaveBeenCalledOnce();
    expect(h.failure).not.toHaveBeenCalled();
    h.control.stop();
  });

  it.each([
    ["plain busy-shaped object", { disposition: "busy" }],
    ["typed stale", new AuthoringMediaSourceLeaseError("stale", 409)],
    ["ordinary failure", new Error("synthetic renewal failure")],
  ])(
    "reports %s immediately and cancels the deadline",
    async (_label, error) => {
      const h = clock();
      h.renew.mockRejectedValueOnce(error);
      h.control.start();
      h.fire(30_000);
      await settle();
      expect(h.failure).toHaveBeenCalledOnce();
      expect(h.live()).toEqual([]);
      expect(h.schedule.mock.calls.map(([, delay]) => delay)).toEqual([
        30_000, 30_000,
      ]);
      expect(h.renew.mock.calls[0]![0].aborted).toBe(true);
    },
  );

  it.each([
    new AuthoringMediaSourceLeaseError("busy", 429),
    new Error("late retired renewal failure"),
  ])(
    "ignores an already stopped renewal's asynchronous rejection",
    async (error) => {
      const h = clock();
      let reject!: (error: unknown) => void;
      h.renew.mockImplementationOnce(
        () =>
          new Promise((_resolve, fail) => {
            reject = fail;
          }),
      );
      h.control.start();
      h.fire(30_000);
      const deadline = h.live()[0]!;
      h.control.stop();
      expect(h.renew.mock.calls[0]![0].aborted).toBe(true);
      reject(error);
      await settle();
      deadline.callback();
      expect(h.live()).toEqual([]);
      expect(h.failure).not.toHaveBeenCalled();
      expect(h.schedule).toHaveBeenCalledTimes(2);
    },
  );

  it("retires the active controller before reporting an expired held renewal and ignores its late result", async () => {
    const h = clock();
    let reject!: (error: unknown) => void;
    h.renew.mockImplementationOnce(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail;
        }),
    );
    h.control.start();
    h.fire(30_000);
    const expired = h.fire(30_000);
    expect(h.renew.mock.calls[0]![0].aborted).toBe(true);
    expect(h.failure).toHaveBeenCalledOnce();
    expect(h.live()).toEqual([]);
    reject(new AuthoringMediaSourceLeaseError("busy", 429));
    await settle();
    expired.callback();
    expect(h.failure).toHaveBeenCalledOnce();
    expect(h.schedule).toHaveBeenCalledTimes(2);
  });

  it("stops both an outstanding deadline and busy retry before a new start", async () => {
    const h = clock();
    h.renew.mockRejectedValueOnce(
      new AuthoringMediaSourceLeaseError("busy", 429),
    );
    h.control.start();
    h.fire(30_000);
    await settle();
    const pending = [...h.live()];
    expect(pending).toHaveLength(2);
    h.control.start();
    expect(pending.every((job) => !job.live)).toBe(true);
    expect(h.live().map((job) => job.delay)).toEqual([30_000]);
    h.control.stop();
  });

  it("ignores a queued busy callback delivered after a public restart", async () => {
    const h = clock();
    h.renew.mockRejectedValueOnce(
      new AuthoringMediaSourceLeaseError("busy", 429),
    );
    h.control.start();
    h.fire(30_000);
    await settle();
    const queued = h.live().find((job) => job.delay === 250)!;
    h.control.start();
    queued.callback();
    await settle();
    expect(h.renew).toHaveBeenCalledOnce();
    expect(h.failure).not.toHaveBeenCalled();
    h.fire(30_000);
    await settle();
    expect(h.renew).toHaveBeenCalledTimes(2);
    expect(h.renew.mock.calls[1]![0].aborted).toBe(false);
    expect(h.renew.mock.calls[1]![0]).not.toBe(h.renew.mock.calls[0]![0]);
    h.control.stop();
  });
});
