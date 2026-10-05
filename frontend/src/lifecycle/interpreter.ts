import type {
  AppModeEffect,
  AppModeEvent,
  AppModeMachine,
  AppModeSnapshot,
} from "./appModeMachine";
import { isAppModeTerminal } from "./appModeMachine";

export type AppModeInspection =
  | Readonly<{ type: "event"; run: number; name: AppModeEvent["type"] }>
  | Readonly<{
      type: "transition";
      run: number;
      event: AppModeEvent["type"];
      from: AppModeSnapshot["value"];
      to: AppModeSnapshot["value"];
    }>
  | Readonly<{
      type: "effect";
      run: number;
      name: AppModeEffect["type"];
      owner: AppModeEffect["owner"];
    }>
  | Readonly<{
      type: "snapshot";
      run: number;
      name: AppModeSnapshot["value"];
    }>
  | Readonly<{
      type: "stale_result";
      run: number;
      name: AppModeEffect["type"];
    }>;

export type AppModeEffectExecutor = (
  effect: AppModeEffect,
  signal: AbortSignal,
  snapshot: AppModeSnapshot,
) => AppModeEvent | void | Promise<AppModeEvent | void>;

export type AppModeInterpreter = Readonly<{
  begin(
    input: Readonly<{
      route: "new" | "existing" | "replace" | "connect";
      existingGraph: boolean;
    }>,
  ): number;
  complete(run: number): boolean;
  invalidate(): void;
  getRunSequence(): number;
  isCurrent(run: number): boolean;
  signalFor(run: number): AbortSignal | undefined;
  synchronizeRunSequence(minimum: number): void;
  send(event: AppModeEvent): AppModeSnapshot;
  getSnapshot(): AppModeSnapshot;
  settled(): Promise<void>;
  dispose(): void;
}>;

type Options = Readonly<{
  machine: AppModeMachine;
  execute?: AppModeEffectExecutor;
  inspect?: (entry: AppModeInspection) => void;
  onInvalidate?: () => void;
  onDispose?: () => void;
}>;

type Invocation = Readonly<{
  run: number;
  state: AppModeSnapshot["value"];
  effect: AppModeEffect;
  controller: AbortController;
}>;

const invokedEffectTypes = new Set<AppModeEffect["type"]>([
  "queue",
  "write",
  "restore",
  "readProjection",
]);

export function createAppModeInterpreter(options: Options): AppModeInterpreter {
  let current = options.machine.initial;
  let disposed = false;
  let sequence = 0;
  let runController: AbortController | undefined;
  const invocations = new Set<Invocation>();
  const pending = new Set<Promise<void>>();

  const inspect = (entry: AppModeInspection): void => {
    try {
      options.inspect?.(Object.freeze(entry));
    } catch {
      // CRITICAL: diagnostics cannot change lifecycle behavior or make a run fail.
    }
  };

  const abortExitedInvocations = (
    next: AppModeSnapshot,
    previous: AppModeSnapshot,
  ): void => {
    if (
      next.value === previous.value &&
      next.context.run === previous.context.run
    )
      return;
    for (const invocation of invocations)
      if (
        invocation.state !== next.value ||
        invocation.run !== next.context.run
      )
        invocation.controller.abort();
  };

  const executeEffect = (
    effect: AppModeEffect,
    launched: AppModeSnapshot,
  ): void => {
    inspect({
      type: "effect",
      run: launched.context.run,
      name: effect.type,
      owner: effect.owner,
    });
    if (options.execute === undefined) return;

    const controller = new AbortController();
    const invoked = invokedEffectTypes.has(effect.type);
    const invocation: Invocation = Object.freeze({
      run: launched.context.run,
      state: launched.value,
      effect,
      controller,
    });
    if (invoked) invocations.add(invocation);

    let execution: AppModeEvent | void | Promise<AppModeEvent | void>;
    try {
      execution = options.execute(effect, controller.signal, launched);
    } catch (error) {
      execution = Promise.reject(error);
    }
    const task = Promise.resolve(execution)
      .then((event) => {
        if (event === undefined) return;
        if (
          disposed ||
          controller.signal.aborted ||
          current.context.run !== invocation.run
        ) {
          inspect({
            type: "stale_result",
            run: invocation.run,
            name: effect.type,
          });
          return;
        }
        send(event);
      })
      .catch(() => {
        if (
          disposed ||
          controller.signal.aborted ||
          current.context.run !== invocation.run
        )
          return;
        send({ type: "FAILED", code: "internal_failure", recovery: "retry" });
      })
      .finally(() => {
        invocations.delete(invocation);
      });
    pending.add(task);
    void task.finally(() => pending.delete(task));
  };

  function send(event: AppModeEvent): AppModeSnapshot {
    if (disposed) return current;
    if (event.type === "START") {
      sequence = Math.max(sequence, event.run);
      if (current.context.run !== event.run) runController?.abort();
      if (current.context.run !== event.run || runController === undefined)
        runController = new AbortController();
    }
    const previous = current;
    inspect({
      type: "event",
      run: event.type === "START" ? event.run : current.context.run,
      name: event.type,
    });
    const result = options.machine.transition(current, event);
    if (!result.accepted) return current;
    current = result.snapshot;
    abortExitedInvocations(current, previous);
    inspect({
      type: "transition",
      run: current.context.run,
      event: event.type,
      from: previous.value,
      to: current.value,
    });
    for (const effect of result.effects) executeEffect(effect, current);
    inspect({
      type: "snapshot",
      run: current.context.run,
      name: current.value,
    });
    return current;
  }

  return Object.freeze({
    begin(input): number {
      if (isAppModeTerminal(current.value)) send({ type: "RESET" });
      sequence += 1;
      send({ type: "START", run: sequence, ...input });
      return sequence;
    },
    complete(run: number): boolean {
      if (
        disposed ||
        sequence !== run ||
        current.context.run !== run ||
        current.value !== "terminal.done" ||
        runController === undefined ||
        runController.signal.aborted
      )
        return false;
      // CRITICAL: normal child completion retires only its run token. Calling invalidate
      // here would detach the serial parent before its artifact join or next child.
      // Explicit cancellation, host/view loss and disposal keep their separate revoke hooks.
      sequence += 1;
      runController.abort();
      runController = undefined;
      for (const invocation of invocations) invocation.controller.abort();
      return true;
    },
    invalidate(): void {
      if (disposed) return;
      // CRITICAL: stop serial successor authority before aborting the current App Mode run.
      // Deferring this hook lets an already-resolved terminal schedule the next host write/queue.
      try {
        options.onInvalidate?.();
      } catch {
        // The run token still must be revoked if the best-effort detach signal fails.
      }
      send({ type: "CANCEL" });
      sequence += 1;
      runController?.abort();
      runController = undefined;
      for (const invocation of invocations) invocation.controller.abort();
    },
    getRunSequence(): number {
      return sequence;
    },
    isCurrent(run: number): boolean {
      return (
        !disposed &&
        sequence === run &&
        runController !== undefined &&
        !runController.signal.aborted
      );
    },
    signalFor(run: number): AbortSignal | undefined {
      return sequence === run ? runController?.signal : undefined;
    },
    synchronizeRunSequence(minimum: number): void {
      if (
        !disposed &&
        Number.isSafeInteger(minimum) &&
        minimum >= 0 &&
        minimum > sequence
      )
        sequence = minimum;
    },
    send,
    getSnapshot: () => current,
    async settled(): Promise<void> {
      while (pending.size > 0) await Promise.all([...pending]);
    },
    dispose(): void {
      if (disposed) return;
      disposed = true;
      try {
        options.onDispose?.();
      } catch {
        // Disposal and local effect revocation cannot depend on backend reachability.
      }
      runController?.abort();
      runController = undefined;
      for (const invocation of invocations) invocation.controller.abort();
      invocations.clear();
    },
  });
}
