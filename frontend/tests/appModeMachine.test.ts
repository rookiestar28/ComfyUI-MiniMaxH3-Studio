import { describe, expect, it } from "vitest";

import {
  APP_MODE_TERMINAL_STATES,
  APP_MODE_EVENT_TYPES,
  APP_MODE_STATE_VALUES,
  analyzeAppModeGraph,
  createAppModeMachine,
  replayAppModeEvents,
  type AppModeEvent,
} from "../src/lifecycle/appModeMachine";

describe("App Mode statechart", () => {
  it("completes a prepared canvas without scheduling bootstrap or queue work", () => {
    const machine = createAppModeMachine();
    let current = machine.initial;
    const effects: string[] = [];
    for (const event of [
      { type: "START", run: 1, route: "replace", existingGraph: false },
      { type: "CENSUS_RESOLVED", decision: "dirty" },
      { type: "DECISION_ACCEPTED" },
      { type: "VALIDATED", executionIdentity: "prepared-canvas" },
      {
        type: "CANVAS_READY",
        ownedNodeIds: ["1"],
        ownedLinkIds: ["2"],
        ownedProjectionFingerprint: "sha256:owned",
      },
    ] as AppModeEvent[]) {
      const result = machine.transition(current, event);
      current = result.snapshot;
      effects.push(...result.effects.map((effect) => effect.name));
    }
    expect(current.value).toBe("terminal.done");
    expect(current.context).toMatchObject({
      existingGraph: true,
      ownedNodeIds: ["1"],
      ownedLinkIds: ["2"],
    });
    expect(current.context.promptId).toBeUndefined();
    expect(effects).not.toContain("prepare_bootstrap");
    expect(effects).not.toContain("submit_exact_prompt");
  });

  it("collapses managed preparation to one state and one readiness event", () => {
    expect(APP_MODE_STATE_VALUES).toHaveLength(14);
    expect(APP_MODE_STATE_VALUES).toContain("preparing");
    // `not.toEqual(arrayContaining([a, b, c]))` only fails when every one of the three survives,
    // so a single leftover preparation state would pass it. Assert each collapsed name on its own.
    for (const collapsed of [
      "preparing.bootstrap",
      "preparing.workspace",
      "preparing.sequence",
    ])
      expect(APP_MODE_STATE_VALUES).not.toContain(collapsed);
    expect(APP_MODE_EVENT_TYPES).toContain("PREPARATION_READY");
    for (const collapsed of [
      "BOOTSTRAP_READY",
      "WORKSPACE_READY",
      "SEQUENCE_READY",
    ])
      expect(APP_MODE_EVENT_TYPES).not.toContain(collapsed);
  });

  it("makes every terminal reachable and keeps forbidden paths absent", () => {
    const report = analyzeAppModeGraph();

    expect(report.unreachableTerminals).toEqual([]);
    expect(report.forbiddenPaths).toEqual([]);
    expect(report.reachableStates).toEqual(
      expect.arrayContaining([...APP_MODE_TERMINAL_STATES]),
    );
  });

  it("emits effects as closed data while retaining the three run identities", () => {
    const machine = createAppModeMachine();
    let current = machine.initial;
    const effects: string[] = [];
    const send = (event: AppModeEvent) => {
      const result = machine.transition(current, event);
      current = result.snapshot;
      effects.push(...result.effects.map((effect) => effect.type));
    };

    send({ type: "START", run: 7, route: "new", existingGraph: false });
    send({ type: "CENSUS_RESOLVED", decision: "empty" });
    send({ type: "DECISION_ACCEPTED" });
    send({ type: "VALIDATED", executionIdentity: "execution-7" });
    send({
      type: "WRITTEN",
      ownedNodeIds: ["10", "11"],
      ownedLinkIds: ["20"],
      ownedProjectionFingerprint: "sha256:owned",
      surroundingsDigest: "sha256:surroundings",
    });
    send({ type: "PREPARATION_READY" });
    send({ type: "QUEUE_ACCEPTED", promptId: "prompt-7" });
    send({ type: "EXECUTION_STARTED" });
    send({ type: "EXECUTION_FINISHED" });
    send({ type: "OUTPUT_VERIFIED" });

    expect(current.value).toBe("terminal.done");
    expect(current.context).toMatchObject({
      run: 7,
      executionIdentity: "execution-7",
      ownedNodeIds: ["10", "11"],
      ownedLinkIds: ["20"],
      ownedProjectionFingerprint: "sha256:owned",
      surroundingsDigest: "sha256:surroundings",
      promptId: "prompt-7",
    });
    expect(effects).toEqual(
      expect.arrayContaining([
        "readProjection",
        "write",
        "queue",
        "journal",
        "present",
      ]),
    );
  });

  it("records the prompt binding when execution starts before queue acceptance", () => {
    const running = replayAppModeEvents([
      { type: "START", run: 9, route: "new", existingGraph: false },
      { type: "CENSUS_RESOLVED", decision: "empty" },
      { type: "DECISION_ACCEPTED" },
      { type: "VALIDATED", executionIdentity: "execution-9" },
      {
        type: "WRITTEN",
        ownedNodeIds: ["1"],
        ownedLinkIds: [],
        ownedProjectionFingerprint: "sha256:owned-9",
      },
      { type: "PREPARATION_READY" },
      { type: "EXECUTION_STARTED" },
    ]).snapshot;
    expect(running.value).toBe("running");
    expect(running.context.promptId).toBeUndefined();

    const machine = createAppModeMachine();
    const bound = machine.transition(running, {
      type: "QUEUE_ACCEPTED",
      promptId: "prompt-9",
    });
    expect(bound.accepted).toBe(true);
    expect(bound.snapshot.value).toBe("running");
    expect(bound.snapshot.context.promptId).toBe("prompt-9");

    const second = machine.transition(bound.snapshot, {
      type: "QUEUE_ACCEPTED",
      promptId: "prompt-10",
    });
    expect(second.accepted).toBe(false);
    expect(second.snapshot.context.promptId).toBe("prompt-9");
  });

  it("wraps a host interruption and restores the exact prior state", () => {
    const machine = createAppModeMachine();
    const started = machine.transition(machine.initial, {
      type: "START",
      run: 3,
      route: "existing",
      existingGraph: true,
    }).snapshot;
    const lostTransition = machine.transition(started, { type: "HOST_LOST" });
    const lost = lostTransition.snapshot;
    const reconnecting = machine.transition(lost, {
      type: "HOST_RECONNECTING",
    }).snapshot;
    const restored = machine.transition(reconnecting, {
      type: "HOST_RESTORED",
    }).snapshot;

    expect(lost.value).toBe("host_unavailable");
    expect(lostTransition.effects).toContainEqual({
      type: "detach",
      owner: "ManagedSequenceRunner",
      name: "stop_serial_successors",
    });
    expect(reconnecting.context.hostPhase).toBe("reconnecting");
    expect(restored).toEqual(started);
  });

  it("replays the recorded failure shape with an explicit effect list", () => {
    const replay = replayAppModeEvents([
      { type: "START", run: 11, route: "replace", existingGraph: true },
      { type: "CENSUS_RESOLVED", decision: "dirty" },
      { type: "DECISION_ACCEPTED" },
      { type: "VALIDATED", executionIdentity: "execution-11" },
      {
        type: "FAILED",
        code: "stale_graph",
        recovery: "inspect",
      },
    ]);

    expect(replay.snapshot.value).toBe("terminal.failed");
    expect(replay.snapshot.context.failure).toEqual({
      code: "stale_graph",
      recovery: "inspect",
    });
    expect(replay.effects.map((effect) => effect.type)).toEqual(
      expect.arrayContaining(["readProjection", "write", "journal", "present"]),
    );
  });
});
