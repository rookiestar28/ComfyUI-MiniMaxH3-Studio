import type { DurationResolution } from "../host/durationResolutionClient";

export type DurationResolutionState =
  | Readonly<{ status: "resolving"; requestedSeconds: number }>
  | Readonly<{ status: "resolved"; resolution: DurationResolution }>
  | Readonly<{ status: "refused"; requestedSeconds: number }>;

export function createDurationResolutionController(options: {
  resolve(
    requestedSeconds: number,
    signal: AbortSignal,
  ): Promise<DurationResolution>;
  changed(): void;
}) {
  let state: DurationResolutionState | undefined;
  let generation = 0;
  let activeAbort: AbortController | undefined;

  const currentRequestedSeconds = (): number | undefined =>
    state?.status === "resolved"
      ? state.resolution.requested_seconds
      : state?.requestedSeconds;

  const begin = (requestedSeconds: number, force: boolean): void => {
    if (!force && currentRequestedSeconds() === requestedSeconds) return;
    const run = ++generation;
    activeAbort?.abort();
    const abort = new AbortController();
    activeAbort = abort;
    state = Object.freeze({ status: "resolving", requestedSeconds });
    options.changed();
    void options
      .resolve(requestedSeconds, abort.signal)
      .then((resolution) => {
        if (
          run !== generation ||
          abort.signal.aborted ||
          resolution.requested_seconds !== requestedSeconds
        )
          return;
        state = Object.freeze({ status: "resolved", resolution });
        options.changed();
      })
      .catch(() => {
        if (run !== generation || abort.signal.aborted) return;
        state = Object.freeze({ status: "refused", requestedSeconds });
        options.changed();
      });
  };

  return {
    snapshot(): DurationResolutionState | undefined {
      return state;
    },
    request(requestedSeconds: number): void {
      begin(requestedSeconds, false);
    },
    retry(): void {
      const requestedSeconds = currentRequestedSeconds();
      if (requestedSeconds !== undefined) begin(requestedSeconds, true);
    },
    dispose(): void {
      generation += 1;
      activeAbort?.abort();
      activeAbort = undefined;
      state = undefined;
    },
  };
}
