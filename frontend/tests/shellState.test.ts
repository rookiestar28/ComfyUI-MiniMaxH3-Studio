import { describe, expect, it } from "vitest";

import type { GraphInspection } from "../src/host/graphAdapter";
import { createAppModeMachine } from "../src/lifecycle/appModeMachine";
import {
  hasExistingGraphAuthority,
  initialShellState,
  projectAppModeSnapshot,
  retainedProjectionOwnsPrompt,
  reduceShellState,
} from "../src/state/shellState";

describe("shell state", () => {
  it("projects App Mode machine states into the existing ShellState contract", () => {
    const machine = createAppModeMachine();
    const census = machine.transition(machine.initial, {
      type: "START",
      run: 14,
      route: "existing",
      existingGraph: true,
    }).snapshot;
    const deciding = machine.transition(census, {
      type: "CENSUS_RESOLVED",
      decision: "existing",
    }).snapshot;
    const validating = machine.transition(deciding, {
      type: "DECISION_ACCEPTED",
    }).snapshot;

    expect(projectAppModeSnapshot(census, initialShellState)).toEqual({
      status: "working",
      phase: "compiling",
      transactionId: 14,
      existingGraph: true,
    });
    expect(projectAppModeSnapshot(validating, initialShellState)).toEqual({
      status: "working",
      phase: "compiling",
      transactionId: 14,
      existingGraph: true,
    });
  });

  /**
   * M23-25. A detached admission refusal leaves the current canvas untouched.
   * Graph census events therefore cannot erase the error or claim that the
   * refused candidate has been repaired on-canvas.
   */
  const admissionRefusal = {
    status: "error",
    code: "incompatible_seam",
    severity: "error",
    source: "seam",
    message: "incompatible_seam",
    recovery: "use_native",
    existingGraph: true,
  } as const;
  const readyGraph: GraphInspection = {
    status: "ready",
    anchors: [
      { executionId: "8", nodeId: "comfyui_h3_context.H3Context.ProductShell" },
    ],
    nodeCount: 24,
    existingGraphCompatible: true,
  };

  it("classifies existing-graph authority across every shell lifecycle family", () => {
    expect(
      hasExistingGraphAuthority({
        status: "interactive",
        reason: "native_preference",
        existingGraph: true,
      }),
    ).toBe(true);
    expect(
      hasExistingGraphAuthority({
        status: "interactive",
        reason: "empty_canvas",
      }),
    ).toBe(false);
    expect(
      hasExistingGraphAuthority({
        status: "working",
        phase: "generating",
        transactionId: 7,
        existingGraph: true,
      }),
    ).toBe(true);
    expect(
      hasExistingGraphAuthority({
        status: "working",
        phase: "materializing",
        transactionId: 8,
        existingGraph: false,
      }),
    ).toBe(false);
    expect(
      hasExistingGraphAuthority({
        status: "error",
        code: "queue_failed",
        severity: "error",
        source: "queue",
        message: "queue_failed",
        recovery: "retry",
        existingGraph: true,
      }),
    ).toBe(true);
    expect(
      hasExistingGraphAuthority({
        status: "projected",
        projection: {} as never,
      }),
    ).toBe(false);
    expect(
      hasExistingGraphAuthority({
        status: "editing_setup",
        prior: {
          projection: {} as never,
          anchorExecutionId: "17",
          graphFingerprint: "graph-17",
        },
      }),
    ).toBe(true);
    expect(
      hasExistingGraphAuthority({
        status: "editing_setup",
        prior: {
          projection: {} as never,
          anchorExecutionId: "17",
          graphFingerprint: "",
        },
      }),
    ).toBe(false);
  });

  it("carries a closed refusal reason only through states that retain it", () => {
    const refusalReason = {
      kind: "anchor_missing",
      requiredNode: "MiniMaxH3ImageToVideo",
    } as const;
    const interactive = reduceShellState(initialShellState, {
      type: "interactive",
      reason: "incompatible_graph",
      existingGraph: true,
      refusalReason,
    });
    expect(interactive).toMatchObject({ refusalReason });

    const failure = reduceShellState(interactive, {
      type: "error",
      code: "incompatible_seam",
      source: "seam",
      message: "incompatible_seam",
      recovery: "use_native",
      existingGraph: true,
      refusalReason,
    });
    expect(failure).toMatchObject({ refusalReason });
    expect(
      reduceShellState(failure, {
        type: "graph",
        inspection: { status: "missing", anchors: [], nodeCount: 0 },
      }),
    ).toBe(failure);
    expect(
      reduceShellState(failure, {
        type: "working",
        phase: "materializing",
        transactionId: 1,
      }),
    ).not.toHaveProperty("refusalReason");
    expect(reduceShellState(failure, { type: "reset" })).not.toHaveProperty(
      "refusalReason",
    );
  });

  it("does not let a ready canvas release a detached admission refusal", () => {
    expect(
      reduceShellState(admissionRefusal, {
        type: "graph",
        inspection: readyGraph,
      }),
    ).toBe(admissionRefusal);
  });

  it("keeps the refusal through every unrelated canvas census", () => {
    // Missing, malformed and incompatible rescans are equally unrelated to the
    // detached candidate that failed before the sole write.
    const rescans: GraphInspection[] = [
      { status: "missing", anchors: [], nodeCount: 0 },
      {
        status: "incompatible",
        anchors: [],
        nodeCount: 24,
        reason: "incomplete_h3_flow",
      },
      { ...readyGraph, existingGraphCompatible: false },
    ];
    for (const inspection of rescans)
      expect(
        reduceShellState(admissionRefusal, { type: "graph", inspection }),
      ).toBe(admissionRefusal);
  });

  it("does not let a ready canvas clear an error it is no evidence about", () => {
    // A failed rollback still owns its recovery: the visible graph being
    // adoptable says nothing about the restore that did not happen.
    for (const code of [
      "rollback_failed",
      "compile_failed",
      "queue_failed",
    ] as const) {
      const other = { ...admissionRefusal, code, recovery: "retry" } as const;
      expect(
        reduceShellState(other, { type: "graph", inspection: readyGraph }),
      ).toBe(other);
    }
    // An admission refusal raised before any graph was written can also carry no
    // existing graph authority; a later census still cannot adopt its candidate.
    const setupFailure = { ...admissionRefusal, existingGraph: undefined };
    expect(
      reduceShellState(setupFailure, { type: "graph", inspection: readyGraph }),
    ).toBe(setupFailure);
  });

  it("starts interactive and classifies an empty canvas", () => {
    expect(initialShellState).toMatchObject({
      status: "interactive",
      reason: "pending_capability",
    });
    expect(
      reduceShellState(initialShellState, {
        type: "graph",
        inspection: { status: "missing", anchors: [], nodeCount: 0 },
      }),
    ).toMatchObject({ status: "interactive", reason: "empty_canvas" });
  });

  it("keeps native preference reversible for a compatible visible graph", () => {
    expect(
      reduceShellState(initialShellState, {
        type: "graph",
        inspection: {
          status: "ready",
          anchors: [
            {
              executionId: "8",
              nodeId: "comfyui_h3_context.H3Context.ProductShell",
            },
          ],
          nodeCount: 8,
          existingGraphCompatible: true,
        },
      }),
    ).toMatchObject({
      status: "interactive",
      reason: "native_preference",
      existingGraph: true,
    });
  });

  it("keeps a dirty graph interactive without silent replacement", () => {
    expect(
      reduceShellState(initialShellState, {
        type: "graph",
        inspection: { status: "missing", anchors: [], nodeCount: 3 },
      }),
    ).toMatchObject({ status: "interactive", reason: "dirty_graph" });
  });

  it("projects only when the Product Shell anchor identity matches", () => {
    const projection = {
      correlation: { execution_node_id: "17", prompt_id: "p1" },
    } as never;
    expect(
      reduceShellState(initialShellState, {
        type: "projection",
        anchorExecutionId: "17",
        projection,
      }),
    ).toMatchObject({ status: "projected" });
    expect(
      reduceShellState(initialShellState, {
        type: "projection",
        anchorExecutionId: "18",
        projection,
      }),
    ).toMatchObject({ status: "error", code: "projection_mismatch" });
  });

  it("rejects a projection that does not match the active App Mode transaction", () => {
    const projection = {
      correlation: { execution_node_id: "17", prompt_id: "p1" },
      prompt_fingerprint: "prompt-1",
    } as never;
    const working = reduceShellState(initialShellState, {
      type: "working",
      phase: "queueing",
      transactionId: 7,
      graphFingerprint: "graph-1",
    });
    expect(
      reduceShellState(working, {
        type: "projection",
        anchorExecutionId: "17",
        projection,
        transactionId: 8,
        graphFingerprint: "graph-2",
        backendPromptFingerprint: "prompt-2",
      }),
    ).toMatchObject({ status: "error", code: "projection_mismatch" });
    expect(
      reduceShellState(working, {
        type: "projection",
        anchorExecutionId: "17",
        projection,
        transactionId: 7,
        graphFingerprint: "graph-1",
        backendPromptFingerprint: "prompt-foreign",
      }),
    ).toMatchObject({ status: "error", code: "projection_mismatch" });
    expect(
      reduceShellState(working, {
        type: "projection",
        anchorExecutionId: "17",
        projection,
        transactionId: 7,
        graphFingerprint: "graph-1",
        backendPromptFingerprint: "prompt-1",
      }),
    ).toMatchObject({ status: "projected", transactionId: 7 });
  });

  it("keeps working and error states across non-ready graph rescans", () => {
    const inspection = {
      status: "incompatible" as const,
      reason: "malformed_graph_links",
      anchors: [],
      nodeCount: 4,
    };
    const working = reduceShellState(initialShellState, {
      type: "working",
      phase: "compiling",
      transactionId: 11,
      graphFingerprint: "graph-11",
    });
    expect(reduceShellState(working, { type: "graph", inspection })).toBe(
      working,
    );

    const error = reduceShellState(initialShellState, {
      type: "error",
      code: "queue_failed",
      source: "queue",
      message: "Queue failed.",
      recovery: "retry",
    });
    expect(reduceShellState(error, { type: "graph", inspection })).toBe(error);
  });

  it("classifies every non-ready graph decision without queue authority", () => {
    const cases = [
      {
        inspection: { status: "ambiguous" as const, anchors: [], nodeCount: 2 },
        reason: "ambiguous_graph" as const,
      },
      {
        inspection: {
          status: "incompatible" as const,
          reason: "malformed_graph_links",
          anchors: [],
          nodeCount: 2,
        },
        reason: "malformed_graph" as const,
      },
      {
        inspection: {
          status: "incompatible" as const,
          reason: "incomplete_h3_flow",
          anchors: [],
          nodeCount: 2,
        },
        reason: "incompatible_graph" as const,
      },
    ];
    for (const { inspection, reason } of cases) {
      expect(
        reduceShellState(initialShellState, { type: "graph", inspection }),
      ).toMatchObject({ status: "interactive", reason, existingGraph: true });
    }
  });

  it("returns to interactive after a graph rescan, even when the anchor is unchanged", () => {
    const projection = {
      correlation: { execution_node_id: "17", prompt_id: "p1" },
    } as never;
    const projected = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: "17",
      projection,
      transactionId: 7,
      graphFingerprint: "graph-1",
    });
    expect(
      reduceShellState(projected, {
        type: "graph",
        inspection: {
          status: "ready",
          anchors: [
            {
              executionId: "17",
              nodeId: "comfyui_h3_context.H3Context.ProductShell",
            },
          ],
          nodeCount: 8,
          existingGraphCompatible: true,
        },
      }),
    ).toMatchObject({
      status: "interactive",
      reason: "native_preference",
      existingGraph: true,
    });
  });

  it("keeps a verified projection visible when model-free rescan removes native generation", () => {
    const projection = {
      correlation: { execution_node_id: "17", prompt_id: "p1" },
    } as never;
    const projected = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: "17",
      projection,
      transactionId: 7,
      graphFingerprint: "graph-1",
      modelFreeRescanExpected: true,
    });
    const incomplete = {
      status: "missing" as const,
      reason: "missing_native_h3_core",
      anchors: [
        {
          executionId: "17",
          nodeId: "comfyui_h3_context.H3Context.ProductShell" as const,
        },
      ],
      nodeCount: 7,
    };
    const retained = reduceShellState(projected, {
      type: "graph",
      inspection: incomplete,
    });
    expect(retained).toMatchObject({
      status: "projected",
      modelFreeRescanExpected: false,
    });
    expect(
      reduceShellState(retained, { type: "graph", inspection: incomplete }),
    ).toMatchObject({ status: "interactive", reason: "dirty_graph" });
  });

  it("does not retain a model-free projection without App Mode provenance", () => {
    const projection = {
      correlation: { execution_node_id: "17", prompt_id: "p1" },
    } as never;
    const projected = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: "17",
      projection,
    });
    expect(
      reduceShellState(projected, {
        type: "graph",
        inspection: {
          status: "incompatible",
          reason: "incomplete_h3_flow",
          anchors: [
            {
              executionId: "17",
              nodeId: "comfyui_h3_context.H3Context.ProductShell",
            },
          ],
          nodeCount: 1,
        },
      }),
    ).toMatchObject({ status: "interactive", reason: "incompatible_graph" });
  });

  it("round-trips projected setup editing without losing the prior identity", () => {
    const projection = {
      correlation: { execution_node_id: "17", prompt_id: "p1" },
    } as never;
    const projected = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: "17",
      projection,
      transactionId: 7,
      graphFingerprint: "graph-1",
    });
    const editing = reduceShellState(projected, { type: "edit_setup" });
    expect(editing).toMatchObject({
      status: "editing_setup",
      prior: {
        projection,
        anchorExecutionId: "17",
        graphFingerprint: "graph-1",
        transactionId: 7,
      },
    });

    expect(
      reduceShellState(editing, {
        type: "cancel_edit",
        graphFingerprint: "graph-1",
      }),
    ).toEqual(projected);
  });

  it("fails closed and reclassifies when the graph drifts during setup editing", () => {
    const projection = {
      correlation: { execution_node_id: "17", prompt_id: "p1" },
    } as never;
    const projected = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: "17",
      projection,
      graphFingerprint: "graph-1",
    });
    const editing = reduceShellState(projected, { type: "edit_setup" });
    expect(
      reduceShellState(editing, {
        type: "cancel_edit",
        graphFingerprint: "graph-2",
        inspection: {
          status: "ready",
          anchors: [
            {
              executionId: "18",
              nodeId: "comfyui_h3_context.H3Context.ProductShell",
            },
          ],
          nodeCount: 8,
          existingGraphCompatible: true,
        },
      }),
    ).toMatchObject({
      status: "interactive",
      reason: "native_preference",
      existingGraph: true,
    });
  });

  it("keeps illegal setup-edit transitions as no-ops", () => {
    expect(reduceShellState(initialShellState, { type: "edit_setup" })).toBe(
      initialShellState,
    );
    const projection = {
      correlation: { execution_node_id: "17", prompt_id: "p1" },
    } as never;
    const projected = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: "17",
      projection,
      graphFingerprint: "graph-1",
    });
    expect(
      reduceShellState(projected, {
        type: "cancel_edit",
        graphFingerprint: "graph-1",
      }),
    ).toBe(projected);
  });
});

describe("host availability shell state", () => {
  const readyGraph: GraphInspection = {
    status: "ready",
    anchors: [
      { executionId: "8", nodeId: "comfyui_h3_context.H3Context.ProductShell" },
    ],
    nodeCount: 24,
    existingGraphCompatible: true,
  };
  const working = reduceShellState(initialShellState, {
    type: "working",
    phase: "generating",
    transactionId: 4,
    existingGraph: true,
    graphFingerprint: "graph-1",
  });

  it("wraps the interrupted state instead of replacing it with an error", () => {
    const lost = reduceShellState(working, {
      type: "host_unavailable",
      phase: "lost",
    });
    expect(lost.status).toBe("host_unavailable");
    if (lost.status !== "host_unavailable") throw new Error("unreachable");
    expect(lost.phase).toBe("lost");
    expect(lost.priorStatus).toBe("working");
    expect(lost.prior).toBe(working);
  });

  it("keeps existing-graph authority across the interruption", () => {
    const lost = reduceShellState(working, {
      type: "host_unavailable",
      phase: "lost",
    });
    expect(hasExistingGraphAuthority(lost)).toBe(true);
  });

  it("advances the phase without nesting or losing the original prior state", () => {
    const lost = reduceShellState(working, {
      type: "host_unavailable",
      phase: "lost",
    });
    const reconnecting = reduceShellState(lost, {
      type: "host_unavailable",
      phase: "reconnecting",
    });
    expect(reconnecting.status).toBe("host_unavailable");
    if (reconnecting.status !== "host_unavailable")
      throw new Error("unreachable");
    expect(reconnecting.phase).toBe("reconnecting");
    expect(reconnecting.prior).toBe(working);
  });

  it("restores exactly the state that was interrupted", () => {
    const lost = reduceShellState(working, {
      type: "host_unavailable",
      phase: "lost",
    });
    expect(reduceShellState(lost, { type: "host_restored" })).toBe(working);
  });

  it("ignores a restore that no interruption is holding", () => {
    expect(reduceShellState(working, { type: "host_restored" })).toBe(working);
  });

  it("does not let a graph census erase the interruption", () => {
    const lost = reduceShellState(working, {
      type: "host_unavailable",
      phase: "lost",
    });
    expect(
      reduceShellState(lost, { type: "graph", inspection: readyGraph }),
    ).toBe(lost);
  });

  it("still allows a reconciliation refusal to classify the run", () => {
    const lost = reduceShellState(working, {
      type: "host_unavailable",
      phase: "lost",
    });
    const refused = reduceShellState(lost, {
      type: "error",
      code: "run_authority_mismatch",
      source: "coordinator",
      message: "run_authority_mismatch",
      recovery: "inspect",
      existingGraph: true,
    });
    expect(refused.status).toBe("error");
    if (refused.status !== "error") throw new Error("unreachable");
    expect(refused.code).toBe("run_authority_mismatch");
  });

  it("keeps the retained projection prompt owner visible through the drop", () => {
    const projected = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: "8",
      projection: {
        correlation: { prompt_id: "prompt-9", execution_node_id: "8" },
      } as never,
    });
    const lost = reduceShellState(projected, {
      type: "host_unavailable",
      phase: "lost",
    });
    expect(retainedProjectionOwnsPrompt(lost, "prompt-9")).toBe(true);
    expect(retainedProjectionOwnsPrompt(lost, "prompt-8")).toBe(false);
  });

  it("wraps and restores every state kind a run can be interrupted in", () => {
    const projected = reduceShellState(initialShellState, {
      type: "projection",
      anchorExecutionId: "8",
      projection: {
        correlation: { prompt_id: "prompt-9", execution_node_id: "8" },
      } as never,
      graphFingerprint: "graph-1",
    });
    const editingSetup = reduceShellState(projected, { type: "edit_setup" });
    // `edit_setup` is a no-op unless the prior state is a projection carrying a graph fingerprint,
    // so assert the fixture actually reached the state before the loop claims to cover it.
    expect(editingSetup.status).toBe("editing_setup");
    const failed = reduceShellState(initialShellState, {
      type: "error",
      code: "execution_failed",
      source: "coordinator",
      message: "execution_failed",
      recovery: "retry",
      existingGraph: false,
    });
    for (const prior of [
      initialShellState,
      working,
      projected,
      editingSetup,
      failed,
    ]) {
      const lost = reduceShellState(prior, {
        type: "host_unavailable",
        phase: "lost",
      });
      expect(lost.status).toBe("host_unavailable");
      if (lost.status !== "host_unavailable") throw new Error("unreachable");
      expect(lost.priorStatus).toBe(prior.status);
      expect(reduceShellState(lost, { type: "host_restored" })).toBe(prior);
    }
  });

  it("validates a projection arriving during the drop against the suspended transaction", () => {
    const lost = reduceShellState(working, {
      type: "host_unavailable",
      phase: "lost",
    });
    const projection = {
      correlation: { prompt_id: "prompt-9", execution_node_id: "8" },
      prompt_fingerprint: "sha256:prompt-9",
    } as never;
    // The interrupted `working` holds transaction 4 over graph-1. A projection from another
    // transaction is refused against that suspended run, not waved through because a wrapper sits
    // in front of the guard -- and the refusal names the suspended transaction, not the foreign one.
    const refused = reduceShellState(lost, {
      type: "projection",
      anchorExecutionId: "8",
      projection,
      graphFingerprint: "graph-1",
      backendPromptFingerprint: "sha256:prompt-9",
      transactionId: 5,
    });
    expect(refused).toMatchObject({
      status: "error",
      code: "projection_mismatch",
      transactionId: 4,
    });
    const accepted = reduceShellState(lost, {
      type: "projection",
      anchorExecutionId: "8",
      projection,
      graphFingerprint: "graph-1",
      backendPromptFingerprint: "sha256:prompt-9",
      transactionId: 4,
    });
    expect(accepted).toMatchObject({
      status: "projected",
      transactionId: 4,
    });
  });
});
