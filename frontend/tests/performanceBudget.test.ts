import { describe, expect, it, vi } from "vitest";

import {
  MAX_RENDERED_LIST_ITEMS,
  boundedListWindow,
  createFrontendPerformanceRecorder,
  createGraphRefreshCoalescer,
} from "../src/performance/performanceBudget";

describe("frontend performance budgets", () => {
  it("coalesces a graph burst into one owned refresh", () => {
    const queued: Array<() => void> = [];
    const cancelled = vi.fn();
    const refresh = vi.fn();
    const coalescer = createGraphRefreshCoalescer(refresh, (callback) => {
      queued.push(callback);
      return cancelled;
    });
    coalescer.request();
    coalescer.request();
    coalescer.request();
    expect(queued).toHaveLength(1);
    expect(refresh).not.toHaveBeenCalled();
    queued[0]?.();
    expect(refresh).toHaveBeenCalledOnce();
    coalescer.request();
    expect(queued).toHaveLength(2);
    coalescer.destroy();
    expect(cancelled).toHaveBeenCalledOnce();
    queued[1]?.();
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("invalidates a captured late callback after destroy", () => {
    let callback: (() => void) | undefined;
    const refresh = vi.fn();
    const coalescer = createGraphRefreshCoalescer(refresh, (next) => {
      callback = next;
      return vi.fn();
    });
    coalescer.request();
    const late = callback;
    coalescer.destroy();
    late?.();
    expect(refresh).not.toHaveBeenCalled();
    expect(coalescer.pending()).toBe(false);
  });

  it("windows a large list without changing total order or active ownership", () => {
    const values = Array.from({ length: 100 }, (_, index) => `item-${index}`);
    const first = boundedListWindow(values, -1);
    expect(first.start).toBe(0);
    expect(first.items).toEqual(values.slice(0, MAX_RENDERED_LIST_ITEMS));
    const middle = boundedListWindow(values, 65);
    expect(middle.items.length).toBeLessThanOrEqual(MAX_RENDERED_LIST_ITEMS);
    expect(middle.start).toBe(64);
    expect(middle.items[1]).toBe("item-65");
    expect(middle.total).toBe(100);
    expect(middle.activeIndex).toBe(65);
  });

  it("rejects invalid list windows instead of allocating unbounded output", () => {
    expect(() => boundedListWindow(Array(10_001).fill("x"), 0)).toThrow(
      /item bound/,
    );
    expect(() => boundedListWindow(["a"], 2)).toThrow(/active index/);
  });

  it("retains bounded aggregate timings and never retains measured content", () => {
    const times = [1, 3, 5, 9];
    const recorder = createFrontendPerformanceRecorder({
      now: () => times.shift() ?? 9,
    });
    expect(
      recorder.measure("decode", () => ({ prompt: "private prompt text" })),
    ).toEqual({ prompt: "private prompt text" });
    recorder.recordProjectionBytes({ prompt: "private prompt text" });
    recorder.recordGraphEvent();
    recorder.recordGraphEvent();
    recorder.measure("refresh", () => undefined);
    recorder.recordRefresh();
    const receipt = recorder.receipt();
    expect(receipt).toMatchObject({
      schema: "h3.frontend.performance.v1",
      decode_ms: 2,
      refresh_ms: 4,
      graph_event_count: 2,
      refresh_count: 1,
      coalesced_event_count: 1,
      cleanup_verified: false,
    });
    expect(receipt.projection_peak_bytes).toBeGreaterThan(0);
    expect(JSON.stringify(receipt)).not.toContain("private prompt text");
    recorder.destroy();
    expect(recorder.receipt().cleanup_verified).toBe(true);
  });

  it("rejects oversized projection measurement before retaining a receipt", () => {
    const recorder = createFrontendPerformanceRecorder();
    expect(() =>
      recorder.recordProjectionBytes({ value: "x".repeat(131_073) }),
    ).toThrow(/projection byte budget/);
    expect(recorder.receipt().projection_peak_bytes).toBe(0);
  });

  it("keeps behavior available while marking unsupported browser timing unavailable", () => {
    const recorder = createFrontendPerformanceRecorder({
      now: () => Number.NaN,
      timing: null,
    });
    expect(recorder.measure("render", () => "rendered")).toBe("rendered");
    expect(recorder.receipt()).toMatchObject({
      render_ms: null,
      diagnostic_codes: [
        "browser_user_timing_unavailable",
        "render_duration_unavailable",
      ],
    });
  });
});
