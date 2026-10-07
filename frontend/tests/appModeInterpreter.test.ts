import { describe, expect, it, vi } from "vitest";

import {
  createAppModeMachine,
  type AppModeEffect,
  type AppModeEvent,
} from "../src/lifecycle/appModeMachine";
import { createAppModeInterpreter } from "../src/lifecycle/interpreter";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

describe("App Mode effect interpreter", () => {
  it("retires only an exact completed child while preserving explicit parent revocation", () => {
    const onInvalidate = vi.fn();
    const interpreter = createAppModeInterpreter({
      machine: createAppModeMachine(),
      onInvalidate,
    });
    const run = interpreter.begin({ route: "new", existingGraph: false });
    const signal = interpreter.signalFor(run);
    expect(interpreter.complete(run)).toBe(false);
    expect(interpreter.isCurrent(run)).toBe(true);
    for (const event of [
      { type: "CENSUS_RESOLVED", decision: "empty" },
      { type: "DECISION_ACCEPTED" },
      { type: "VALIDATED", executionIdentity: "execution.1" },
      {
        type: "WRITTEN",
        ownedNodeIds: ["1"],
        ownedLinkIds: [],
        ownedProjectionFingerprint: "owned.1",
      },
      { type: "PREPARATION_READY" },
      { type: "EXECUTION_STARTED" },
      { type: "EXECUTION_FINISHED" },
      { type: "OUTPUT_VERIFIED" },
    ] satisfies AppModeEvent[])
      interpreter.send(event);
    expect(interpreter.getSnapshot().value).toBe("terminal.done");
    expect(interpreter.complete(run + 1)).toBe(false);
    expect(signal?.aborted).toBe(false);
    expect(interpreter.complete(run)).toBe(true);
    expect(signal?.aborted).toBe(true);
    expect(interpreter.isCurrent(run)).toBe(false);
    expect(onInvalidate).not.toHaveBeenCalled();
    expect(interpreter.complete(run)).toBe(false);
    interpreter.invalidate();
    expect(onInvalidate).toHaveBeenCalledOnce();
    const next = interpreter.begin({ route: "existing", existingGraph: true });
    expect(interpreter.complete(run)).toBe(false);
    expect(interpreter.isCurrent(next)).toBe(true);
    interpreter.invalidate();
    expect(onInvalidate).toHaveBeenCalledTimes(2);
    expect(interpreter.isCurrent(next)).toBe(false);
    interpreter.dispose();
    expect(interpreter.complete(next)).toBe(false);
  });

  it("stops managed successors synchronously on invalidation and disposal", () => {
    const onInvalidate = vi.fn();
    const onDispose = vi.fn();
    const interpreter = createAppModeInterpreter({
      machine: createAppModeMachine(),
      onInvalidate,
      onDispose,
    });

    interpreter.begin({ route: "new", existingGraph: false });
    interpreter.invalidate();
    expect(onInvalidate).toHaveBeenCalledOnce();
    interpreter.dispose();
    interpreter.dispose();
    expect(onDispose).toHaveBeenCalledOnce();
  });

  it("owns the monotonic run token and root abort signal", () => {
    const interpreter = createAppModeInterpreter({
      machine: createAppModeMachine(),
    });
    const first = interpreter.begin({ route: "new", existingGraph: false });
    const signal = interpreter.signalFor(first);

    expect(first).toBe(1);
    expect(interpreter.isCurrent(first)).toBe(true);
    expect(interpreter.getRunSequence()).toBe(1);
    expect(signal?.aborted).toBe(false);

    interpreter.invalidate();
    expect(signal?.aborted).toBe(true);
    expect(interpreter.isCurrent(first)).toBe(false);
    expect(interpreter.begin({ route: "existing", existingGraph: true })).toBe(
      3,
    );
    expect(interpreter.getRunSequence()).toBe(3);
  });

  it("resumes above a restored journal sequence and resets a terminal snapshot", () => {
    const interpreter = createAppModeInterpreter({
      machine: createAppModeMachine(),
    });
    interpreter.synchronizeRunSequence(12);
    const failed = interpreter.begin({ route: "new", existingGraph: false });
    interpreter.send({
      type: "FAILED",
      code: "compile_failed",
      recovery: "retry",
    });

    expect(failed).toBe(13);
    expect(interpreter.getSnapshot().value).toBe("terminal.failed");
    expect(interpreter.begin({ route: "existing", existingGraph: true })).toBe(
      14,
    );
    expect(interpreter.getSnapshot()).toMatchObject({
      value: "census",
      context: { run: 14, route: "existing" },
    });
  });

  it("drops a superseded run result and aborts that step", async () => {
    const first = deferred<AppModeEvent>();
    const signals: AbortSignal[] = [];
    const inspect = vi.fn();
    const interpreter = createAppModeInterpreter({
      machine: createAppModeMachine(),
      inspect,
      execute: async (effect, signal) => {
        if (effect.type !== "readProjection") return;
        signals.push(signal);
        return signals.length === 1 ? first.promise : undefined;
      },
    });

    interpreter.send({
      type: "START",
      run: 1,
      route: "new",
      existingGraph: false,
    });
    interpreter.send({
      type: "START",
      run: 2,
      route: "new",
      existingGraph: false,
    });
    expect(signals[0]?.aborted).toBe(true);

    first.resolve({ type: "CENSUS_RESOLVED", decision: "empty" });
    await interpreter.settled();

    expect(interpreter.getSnapshot()).toMatchObject({
      value: "census",
      context: { run: 2 },
    });
    expect(inspect).toHaveBeenCalledWith(
      expect.objectContaining({ type: "stale_result", run: 1 }),
    );
  });

  it("aborts an invoked step when an external transition exits its state", () => {
    let signal: AbortSignal | undefined;
    const interpreter = createAppModeInterpreter({
      machine: createAppModeMachine(),
      execute: (effect, nextSignal) => {
        if (effect.type === "readProjection") signal = nextSignal;
        return new Promise<AppModeEvent>(() => undefined);
      },
    });

    interpreter.send({
      type: "START",
      run: 4,
      route: "new",
      existingGraph: false,
    });
    interpreter.send({
      type: "FAILED",
      code: "compile_failed",
      recovery: "retry",
    });

    expect(signal?.aborted).toBe(true);
    expect(interpreter.getSnapshot().value).toBe("terminal.failed");
  });

  it("inspects only event, transition, effect and snapshot names plus run tokens", () => {
    const entries: unknown[] = [];
    const interpreter = createAppModeInterpreter({
      machine: createAppModeMachine(),
      inspect: (entry) => entries.push(entry),
      execute: (_effect: AppModeEffect) => undefined,
    });

    interpreter.send({
      type: "START",
      run: 9,
      route: "connect",
      existingGraph: true,
    });

    expect(entries.map((entry) => (entry as { type: string }).type)).toEqual([
      "event",
      "transition",
      "effect",
      "effect",
      "effect",
      "snapshot",
    ]);
    const wire = JSON.stringify(entries);
    expect(wire).not.toContain("existingGraph");
    expect(wire).not.toContain("payload");
    expect(wire).toContain('"run":9');
    expect(entries).toContainEqual(
      expect.objectContaining({
        type: "transition",
        event: "START",
        from: "idle",
        to: "census",
      }),
    );
  });
});
