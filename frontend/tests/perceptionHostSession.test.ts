import { afterEach, describe, expect, it, vi } from "vitest";
import type { PerceptionHostStatus } from "../src/contracts/perceptionHostCodec";
import type { PerceptionHostReadResult } from "../src/host/perceptionHostClient";
import { createPerceptionHostSession } from "../src/lifecycle/perceptionHostSession";

const visual: PerceptionHostStatus = {
  schema: "h3.context.perception_host_status.v1",
  state: "configured",
  visual: true,
  audio: false,
  reason: "none",
  cancellation_observed: true,
};
const missing: PerceptionHostStatus = {
  ...visual,
  state: "not_configured",
  visual: false,
  reason: "settings_missing",
  cancellation_observed: false,
};
const settle = async () => {
  await Promise.resolve();
  await Promise.resolve();
};
afterEach(() => vi.useRealTimers());

describe("Settings perception read ownership", () => {
  it("reads once on first ensure and only refreshes through explicit action", async () => {
    const read = vi.fn(async (): Promise<PerceptionHostReadResult> => ({
      ok: true,
      status: visual,
    }));
    const changed = vi.fn();
    const session = createPerceptionHostSession({ client: { read }, changed });
    expect(read).not.toHaveBeenCalled();
    session.binding().ensure();
    session.binding().ensure();
    await settle();
    expect(read).toHaveBeenCalledTimes(1);
    expect(session.binding().state).toEqual({
      status: visual,
      busy: false,
      error: null,
    });
    session.binding().ensure();
    expect(read).toHaveBeenCalledTimes(1);
    read.mockResolvedValue({ ok: true, status: missing });
    await session.binding().refresh();
    expect(read).toHaveBeenCalledTimes(2);
    expect(session.binding().state.status).toEqual(missing);
    session.release();
  });

  it("has no timer polling and preserves an observed result across page leave", async () => {
    vi.useFakeTimers();
    const read = vi.fn(async (): Promise<PerceptionHostReadResult> => ({
      ok: true,
      status: visual,
    }));
    const session = createPerceptionHostSession({
      client: { read },
      changed() {},
    });
    session.binding().ensure();
    await settle();
    expect(read).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(1);
    session.binding().leave();
    await session.binding().refresh();
    expect(read).toHaveBeenCalledTimes(1);
    session.binding().ensure();
    expect(read).toHaveBeenCalledTimes(1);
    expect(session.binding().state.status).toEqual(visual);
    session.release();
  });

  it.each(["leave", "release"] as const)(
    "aborts a pending read on %s and drops its late result",
    async (action) => {
      let complete!: (result: PerceptionHostReadResult) => void;
      const read = vi.fn(
        (_signal?: AbortSignal) =>
          new Promise<PerceptionHostReadResult>((done) => {
            complete = done;
          }),
      );
      const changed = vi.fn();
      const session = createPerceptionHostSession({
        client: { read },
        changed,
      });
      session.binding().ensure();
      expect(read).toHaveBeenCalledTimes(1);
      const signal = read.mock.calls[0]![0];
      expect(signal?.aborted).toBe(false);
      if (action === "leave") session.binding().leave();
      else session.release();
      expect(signal?.aborted).toBe(true);
      const count = changed.mock.calls.length;
      complete({ ok: true, status: visual });
      await settle();
      expect(session.binding().state.status).toBe(null);
      expect(session.binding().state.busy).toBe(false);
      expect(changed).toHaveBeenCalledTimes(count);
      session.release();
    },
  );

  it("a new page generation cannot receive the prior page's pending response", async () => {
    const completions: ((result: PerceptionHostReadResult) => void)[] = [];
    const read = vi.fn(
      (_signal?: AbortSignal) =>
        new Promise<PerceptionHostReadResult>((done) => completions.push(done)),
    );
    const session = createPerceptionHostSession({
      client: { read },
      changed() {},
    });
    session.binding().ensure();
    expect(read).toHaveBeenCalledTimes(1);
    session.binding().leave();
    session.binding().ensure();
    expect(read).toHaveBeenCalledTimes(2);
    completions[1]!({ ok: true, status: missing });
    await settle();
    completions[0]!({ ok: true, status: visual });
    await settle();
    expect(session.binding().state.status).toEqual(missing);
    session.release();
  });

  it("does not repeat a failed read on ensure but permits an explicit refresh", async () => {
    const read = vi.fn(async (): Promise<PerceptionHostReadResult> => ({
      ok: false,
      code: "transport_failure",
    }));
    const session = createPerceptionHostSession({
      client: { read },
      changed() {},
    });
    session.binding().ensure();
    await settle();
    expect(read).toHaveBeenCalledTimes(1);
    expect(session.binding().state.error).toBe("transport_failure");
    session.binding().ensure();
    await settle();
    expect(read).toHaveBeenCalledTimes(1);
    read.mockResolvedValue({ ok: true, status: visual });
    await session.binding().refresh();
    expect(session.binding().state.error).toBe(null);
    expect(session.binding().state.status).toEqual(visual);
    session.release();
  });

  it("ignores duplicate refresh while busy and cannot restart after release", async () => {
    let complete!: (result: PerceptionHostReadResult) => void;
    const read = vi.fn(
      () =>
        new Promise<PerceptionHostReadResult>((done) => {
          complete = done;
        }),
    );
    const session = createPerceptionHostSession({
      client: { read },
      changed() {},
    });
    session.binding().ensure();
    expect(read).toHaveBeenCalledTimes(1);
    await session.binding().refresh();
    expect(read).toHaveBeenCalledTimes(1);
    session.release();
    complete({ ok: true, status: visual });
    await settle();
    session.binding().ensure();
    await session.binding().refresh();
    expect(read).toHaveBeenCalledTimes(1);
    expect(session.binding().state.status).toBe(null);
  });

  it.each(["leave", "release"] as const)(
    "does not start a read after reentrant %s during busy publication",
    async (action) => {
      const read = vi.fn(async (): Promise<PerceptionHostReadResult> => ({
        ok: true,
        status: visual,
      }));
      let terminate = true;
      const session = createPerceptionHostSession({
        client: { read },
        changed() {
          if (terminate && session.binding().state.busy) {
            terminate = false;
            if (action === "leave") session.binding().leave();
            else session.release();
          }
        },
      });
      session.binding().ensure();
      await settle();
      expect(
        read.mock.calls.length,
        `read_started_after_reentrant_${action}`,
      ).toBe(0);
      expect(session.binding().state.busy).toBe(false);
      expect(session.binding().state.status).toBe(null);
      session.binding().ensure();
      await settle();
      expect(read).toHaveBeenCalledTimes(action === "leave" ? 1 : 0);
      session.release();
    },
  );
});
