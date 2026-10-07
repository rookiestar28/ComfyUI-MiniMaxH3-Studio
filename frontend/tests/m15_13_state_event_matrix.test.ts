import { describe, expect, it } from "vitest";

import {
  initialShellState,
  reduceShellState,
  type ShellAction,
  type ShellState,
} from "../src/state/shellState";

const shellAnchor = {
  executionId: "17",
  nodeId: "comfyui_h3_context.H3Context.ProductShell" as const,
};

function projection(promptFingerprint = "sha256:prompt-1") {
  return {
    correlation: {
      execution_node_id: shellAnchor.executionId,
      prompt_id: "p1",
    },
    prompt_fingerprint: promptFingerprint,
  } as never;
}

function workingState(): ShellState {
  return reduceShellState(initialShellState, {
    type: "working",
    phase: "queueing",
    transactionId: 7,
    graphFingerprint: "sha256:graph-1",
  });
}

function projectedState(): ShellState {
  return reduceShellState(workingState(), {
    type: "projection",
    anchorExecutionId: shellAnchor.executionId,
    projection: projection(),
    transactionId: 7,
    graphFingerprint: "sha256:graph-1",
    modelFreeRescanExpected: true,
    backendPromptFingerprint: "sha256:prompt-1",
  });
}

const graphActions: Array<{
  id: string;
  inspection: NonNullable<
    Extract<ShellAction, { type: "graph" }>["inspection"]
  >;
  reason: string;
}> = [
  {
    id: "SE-01 empty canvas",
    inspection: { status: "missing", anchors: [], nodeCount: 0 },
    reason: "empty_canvas",
  },
  {
    id: "SE-02 dirty canvas",
    inspection: { status: "missing", anchors: [], nodeCount: 3 },
    reason: "dirty_graph",
  },
  {
    id: "SE-03 ambiguous anchors",
    inspection: { status: "ambiguous", anchors: [], nodeCount: 2 },
    reason: "ambiguous_graph",
  },
  {
    id: "SE-04 malformed graph",
    inspection: {
      status: "incompatible",
      reason: "malformed_graph_links",
      anchors: [],
      nodeCount: 2,
    },
    reason: "malformed_graph",
  },
  {
    id: "SE-05 incomplete graph",
    inspection: {
      status: "incompatible",
      reason: "incomplete_h3_flow",
      anchors: [shellAnchor],
      nodeCount: 1,
    },
    reason: "incompatible_graph",
  },
  {
    id: "SE-06 compatible graph",
    inspection: {
      status: "ready",
      anchors: [shellAnchor],
      nodeCount: 8,
      existingGraphCompatible: true,
    },
    reason: "native_preference",
  },
];

describe("M15-13 state/event/invariant matrix", () => {
  it.each(graphActions)(
    "$id remains interactive and classified",
    ({ inspection, reason }) => {
      const next = reduceShellState(initialShellState, {
        type: "graph",
        inspection,
      });
      expect(next.status).toBe("interactive");
      expect(next).toMatchObject({ reason });
      expect(JSON.stringify(next)).not.toMatch(
        /node-only|awaiting projection|assisted reconstruction unavailable/i,
      );
    },
  );

  it.each(["materializing", "compiling", "queueing"] as const)(
    "SE-07 working/$phase survives a graph refresh",
    (phase) => {
      const state = reduceShellState(initialShellState, {
        type: "working",
        phase,
        transactionId: 7,
        graphFingerprint: "sha256:graph-1",
      });
      const next = reduceShellState(state, {
        type: "graph",
        inspection: {
          status: "incompatible",
          reason: "malformed_graph_links",
          anchors: [],
          nodeCount: 2,
        },
      });
      expect(next).toBe(state);
    },
  );

  it("SE-08 classified error survives an intermediate graph refresh", () => {
    const state = reduceShellState(initialShellState, {
      type: "error",
      code: "queue_failed",
      source: "queue",
      message: "The queue failed.",
      recovery: "retry",
    });
    expect(
      reduceShellState(state, {
        type: "graph",
        inspection: { status: "missing", anchors: [], nodeCount: 0 },
      }),
    ).toBe(state);
  });

  it("SE-09 matching projection enters projected exactly once", () => {
    const next = reduceShellState(workingState(), {
      type: "projection",
      anchorExecutionId: shellAnchor.executionId,
      projection: projection(),
      transactionId: 7,
      graphFingerprint: "sha256:graph-1",
      backendPromptFingerprint: "sha256:prompt-1",
    });
    expect(next).toMatchObject({
      status: "projected",
      transactionId: 7,
      graphFingerprint: "sha256:graph-1",
      promptFingerprint: "sha256:prompt-1",
    });
  });

  it.each([
    {
      id: "SE-10 wrong anchor",
      anchorExecutionId: "foreign",
      transactionId: 7,
      graphFingerprint: "sha256:graph-1",
      backendPromptFingerprint: "sha256:prompt-1",
    },
    {
      id: "SE-11 wrong transaction",
      anchorExecutionId: shellAnchor.executionId,
      transactionId: 8,
      graphFingerprint: "sha256:graph-1",
      backendPromptFingerprint: "sha256:prompt-1",
    },
    {
      id: "SE-12 wrong graph",
      anchorExecutionId: shellAnchor.executionId,
      transactionId: 7,
      graphFingerprint: "sha256:graph-foreign",
      backendPromptFingerprint: "sha256:prompt-1",
    },
    {
      id: "SE-13 wrong backend prompt",
      anchorExecutionId: shellAnchor.executionId,
      transactionId: 7,
      graphFingerprint: "sha256:graph-1",
      backendPromptFingerprint: "sha256:prompt-foreign",
    },
  ])("$id becomes a classified projection error", (testCase) => {
    const next = reduceShellState(workingState(), {
      type: "projection",
      anchorExecutionId: testCase.anchorExecutionId,
      projection: projection(),
      transactionId: testCase.transactionId,
      graphFingerprint: testCase.graphFingerprint,
      backendPromptFingerprint: testCase.backendPromptFingerprint,
    });
    expect(next).toMatchObject({
      status: "error",
      code: "projection_mismatch",
      recovery: "inspect",
    });
  });

  it("SE-14 projected state returns to interactive after a compatible rescan", () => {
    const next = reduceShellState(projectedState(), {
      type: "graph",
      inspection: {
        status: "ready",
        anchors: [shellAnchor],
        nodeCount: 8,
        existingGraphCompatible: true,
      },
    });
    expect(next).toMatchObject({
      status: "interactive",
      reason: "native_preference",
      existingGraph: true,
    });
  });

  it("SE-15 only a verified App Mode projection survives model-free removal", () => {
    const projected = projectedState();
    const retained = reduceShellState(projected, {
      type: "graph",
      inspection: {
        status: "missing",
        reason: "missing_native_h3_core",
        anchors: [shellAnchor],
        nodeCount: 7,
      },
    });
    expect(retained).toMatchObject({
      status: "projected",
      modelFreeRescanExpected: false,
    });
    const unproven = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: shellAnchor.executionId,
      projection: projection(),
    });
    expect(
      reduceShellState(unproven, {
        type: "graph",
        inspection: {
          status: "missing",
          reason: "missing_native_h3_core",
          anchors: [shellAnchor],
          nodeCount: 1,
        },
      }),
    ).toMatchObject({ status: "interactive", reason: "dirty_graph" });
    expect(
      reduceShellState(retained, {
        type: "graph",
        inspection: {
          status: "missing",
          reason: "missing_native_h3_core",
          anchors: [shellAnchor],
          nodeCount: 7,
        },
      }),
    ).toMatchObject({ status: "interactive", reason: "dirty_graph" });
  });

  it("SE-16 reset returns to the non-terminal initial state", () => {
    expect(reduceShellState(projectedState(), { type: "reset" })).toMatchObject(
      { status: "interactive", reason: "pending_capability" },
    );
  });
});
