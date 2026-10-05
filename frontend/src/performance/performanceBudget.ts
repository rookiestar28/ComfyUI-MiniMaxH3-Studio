export const GRAPH_REFRESH_WINDOW_MS = 16;
export const MAX_RENDERED_LIST_ITEMS = 32;
export const MAX_LIST_WINDOW_INPUT_ITEMS = 10_000;
export const MAX_FRONTEND_DURATION_MS = 60_000;
export const MAX_FRONTEND_PROJECTION_BYTES = 131_072;
export const MAX_FRONTEND_UPDATE_COUNT = 10_000;

export type FrontendPerformanceOperation =
  "mount" | "refresh" | "decode" | "render";

export type FrontendPerformanceReceipt = {
  schema: "h3.frontend.performance.v1";
  source: "browser_user_timing";
  mount_ms: number | null;
  refresh_ms: number | null;
  decode_ms: number | null;
  render_ms: number | null;
  projection_peak_bytes: number;
  projection_update_count: number;
  graph_event_count: number;
  refresh_count: number;
  coalesced_event_count: number;
  cleanup_verified: boolean;
  diagnostic_codes: readonly string[];
};

export type FrontendPerformanceRecorder = ReturnType<
  typeof createFrontendPerformanceRecorder
>;

type BrowserTiming = Pick<
  Performance,
  "now" | "mark" | "measure" | "clearMarks" | "clearMeasures"
>;

export function createFrontendPerformanceRecorder(options?: {
  now?: () => number;
  timing?: BrowserTiming | null;
}) {
  const timing =
    options?.timing === undefined
      ? typeof globalThis.performance === "object"
        ? globalThis.performance
        : null
      : options.timing;
  const now = options?.now ?? (() => timing?.now() ?? Number.NaN);
  const timings: Record<FrontendPerformanceOperation, number | null> = {
    mount: null,
    refresh: null,
    decode: null,
    render: null,
  };
  const diagnostics = new Set<string>();
  let projectionPeakBytes = 0;
  let projectionUpdateCount = 0;
  let graphEventCount = 0;
  let refreshCount = 0;
  let graphRefreshCount = 0;
  let cleanupVerified = false;

  const increment = (value: number, code: string): number => {
    if (value >= MAX_FRONTEND_UPDATE_COUNT) {
      diagnostics.add(code);
      throw new Error("frontend performance update budget exceeded");
    }
    return value + 1;
  };
  const finiteNow = (): number | null => {
    try {
      const value = now();
      if (Number.isFinite(value) && value >= 0) return value;
    } catch {
      // The fixed diagnostic below is intentionally content-free.
    }
    diagnostics.add("browser_user_timing_unavailable");
    return null;
  };
  const beginUserTiming = (operation: FrontendPerformanceOperation): void => {
    if (timing === null) return;
    try {
      timing.clearMarks(`h3-context:${operation}:start`);
      timing.clearMarks(`h3-context:${operation}:end`);
      timing.clearMeasures(`h3-context:${operation}`);
      timing.mark(`h3-context:${operation}:start`);
    } catch {
      diagnostics.add("browser_user_timing_unavailable");
    }
  };
  const endUserTiming = (operation: FrontendPerformanceOperation): void => {
    if (timing === null) return;
    try {
      timing.mark(`h3-context:${operation}:end`);
      timing.measure(
        `h3-context:${operation}`,
        `h3-context:${operation}:start`,
        `h3-context:${operation}:end`,
      );
      timing.clearMarks(`h3-context:${operation}:start`);
      timing.clearMarks(`h3-context:${operation}:end`);
    } catch {
      diagnostics.add("browser_user_timing_unavailable");
    }
  };

  return {
    measure<T>(operation: FrontendPerformanceOperation, execute: () => T): T {
      const started = finiteNow();
      beginUserTiming(operation);
      cleanupVerified = false;
      try {
        return execute();
      } finally {
        const finished = finiteNow();
        endUserTiming(operation);
        const elapsed =
          started === null || finished === null ? null : finished - started;
        if (
          elapsed === null ||
          elapsed < 0 ||
          elapsed > MAX_FRONTEND_DURATION_MS
        ) {
          timings[operation] = null;
          diagnostics.add(`${operation}_duration_unavailable`);
        } else {
          timings[operation] = elapsed;
        }
      }
    },
    recordProjectionBytes(value: unknown): void {
      let encoded: Uint8Array;
      try {
        const text = JSON.stringify(value);
        if (text === undefined)
          throw new Error("projection is not JSON serializable");
        encoded = new TextEncoder().encode(text);
      } catch {
        diagnostics.add("projection_bytes_unavailable");
        throw new Error("projection byte measurement failed");
      }
      if (encoded.byteLength > MAX_FRONTEND_PROJECTION_BYTES) {
        diagnostics.add("projection_byte_budget_exceeded");
        throw new Error("projection byte budget exceeded");
      }
      projectionPeakBytes = Math.max(projectionPeakBytes, encoded.byteLength);
      projectionUpdateCount = increment(
        projectionUpdateCount,
        "projection_update_budget_exceeded",
      );
      cleanupVerified = false;
    },
    recordGraphEvent(): void {
      graphEventCount = increment(
        graphEventCount,
        "graph_event_budget_exceeded",
      );
      cleanupVerified = false;
    },
    recordRefresh(fromGraphEvent = true): void {
      refreshCount = increment(refreshCount, "refresh_budget_exceeded");
      if (fromGraphEvent)
        graphRefreshCount = increment(
          graphRefreshCount,
          "graph_refresh_budget_exceeded",
        );
      cleanupVerified = false;
    },
    destroy(): void {
      cleanupVerified = true;
    },
    receipt(): FrontendPerformanceReceipt {
      return {
        schema: "h3.frontend.performance.v1",
        source: "browser_user_timing",
        mount_ms: timings.mount,
        refresh_ms: timings.refresh,
        decode_ms: timings.decode,
        render_ms: timings.render,
        projection_peak_bytes: projectionPeakBytes,
        projection_update_count: projectionUpdateCount,
        graph_event_count: graphEventCount,
        refresh_count: refreshCount,
        coalesced_event_count: Math.max(0, graphEventCount - graphRefreshCount),
        cleanup_verified: cleanupVerified,
        diagnostic_codes: [...diagnostics].sort(),
      };
    },
  };
}

export type OwnedSchedule = (callback: () => void) => () => void;

const defaultSchedule: OwnedSchedule = (callback) => {
  const handle = globalThis.setTimeout(callback, GRAPH_REFRESH_WINDOW_MS);
  return () => globalThis.clearTimeout(handle);
};

export function createGraphRefreshCoalescer(
  refresh: () => void,
  schedule: OwnedSchedule = defaultSchedule,
) {
  let destroyed = false;
  let queued = false;
  let generation = 0;
  let cancel: (() => void) | undefined;
  return {
    request(): void {
      if (destroyed || queued) return;
      queued = true;
      const requestGeneration = generation;
      const ownedCancel = schedule(() => {
        if (destroyed || requestGeneration !== generation) return;
        queued = false;
        cancel = undefined;
        refresh();
      });
      if (queued && !destroyed && requestGeneration === generation)
        cancel = ownedCancel;
    },
    pending(): boolean {
      return queued;
    },
    destroy(): void {
      if (destroyed) return;
      destroyed = true;
      generation += 1;
      const pendingCancel = cancel;
      cancel = undefined;
      queued = false;
      pendingCancel?.();
    },
  };
}

export function boundedListWindow<T>(
  input: readonly T[],
  activeIndex: number,
): {
  items: readonly T[];
  start: number;
  total: number;
  activeIndex: number;
} {
  if (!Array.isArray(input) || input.length > MAX_LIST_WINDOW_INPUT_ITEMS)
    throw new Error("list exceeds its item bound");
  if (
    !Number.isInteger(activeIndex) ||
    activeIndex < -1 ||
    activeIndex >= input.length
  )
    throw new Error("active index is outside the list window");
  const start =
    activeIndex < 0
      ? 0
      : Math.floor(activeIndex / MAX_RENDERED_LIST_ITEMS) *
        MAX_RENDERED_LIST_ITEMS;
  return {
    items: input.slice(start, start + MAX_RENDERED_LIST_ITEMS),
    start,
    total: input.length,
    activeIndex,
  };
}
