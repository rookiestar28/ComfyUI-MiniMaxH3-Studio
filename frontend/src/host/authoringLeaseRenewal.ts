import { AuthoringMediaSourceLeaseError } from "./authoringMediaLeaseTransport";

/** One authority deadline survives every busy reply; only a successful renewal starts another. */
export function createAuthoringLeaseRenewal(
  options: Readonly<{
    eligible(): boolean;
    ttlMs(): number;
    renew(signal: AbortSignal): Promise<void>;
    failure(): void;
    schedule(callback: () => void, delayMs: number): unknown;
    cancel(handle: unknown): void;
  }>,
) {
  let timer: unknown;
  let deadline: unknown;
  let active: AbortController | undefined;
  const stop = (completed?: AbortSignal) => {
    if (timer !== undefined) options.cancel(timer);
    if (deadline !== undefined) options.cancel(deadline);
    timer = deadline = undefined;
    const previous = active;
    active = undefined;
    // IMPORTANT: release aborts the scheduled control before joining the lease's serial tail.
    // Otherwise a fetch that never settles prevents authority cleanup from even starting.
    if (previous !== undefined && previous.signal !== completed)
      previous.abort();
  };
  const start = (completed?: AbortSignal) => {
    stop(completed);
    if (!options.eligible()) return;
    const ttlMs = options.ttlMs();
    const delay = Math.max(1, Math.floor(ttlMs / 2));
    timer = options.schedule(() => {
      timer = undefined;
      const controller = new AbortController();
      active = controller;
      deadline = options.schedule(
        () => {
          if (active !== controller || !options.eligible()) return;
          stop();
          options.failure();
        },
        Math.max(1, ttlMs - delay),
      );
      const attempt = () => {
        timer = undefined;
        if (active !== controller || !options.eligible()) return;
        void options.renew(controller.signal).catch((error: unknown) => {
          if (active !== controller) return;
          // CRITICAL: busy is a transient worker refusal, not lost authority. Retry under the
          // original deadline; resetting that deadline would keep an expired lease alive forever.
          if (
            !controller.signal.aborted &&
            error instanceof AuthoringMediaSourceLeaseError &&
            error.disposition === "busy"
          ) {
            timer = options.schedule(attempt, 250);
            return;
          }
          stop();
          options.failure();
        });
      };
      attempt();
    }, delay);
  };
  return Object.freeze({ start, stop });
}
