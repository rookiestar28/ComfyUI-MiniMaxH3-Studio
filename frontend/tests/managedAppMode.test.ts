import { describe, expect, it, vi } from "vitest";

import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import {
  buildManagedAppModePreparation,
  buildManagedSourceIdentityRequest,
  type AppModeCompiledPrompt,
  type AppModeInputs,
} from "../src/host/appMode";
import {
  INPUT_GEOMETRY_RECEIPT_SCHEMA,
  type InputGeometryReceipt,
} from "../src/host/inputGeometry";
import { saveVideoArtifactFromExecuted } from "../src/host/sidebarHost";
import { createAppModeCorrelation } from "../src/lifecycle/appModeCorrelation";
import { createProductionDestinationStore } from "../src/host/productionDestination";
import {
  createShellSession,
  type ActiveManagedRun,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import {
  decodeSequenceCoordinatorResult,
  type SequenceCoordinatorAction,
  type SequenceCoordinatorResult,
} from "../src/host/sequenceCoordinator";
import {
  managedCoordinatorResponse,
  managedLifecycleWires,
} from "./support/managedLifecycleWire";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import {
  validProductShell,
  validSidebarWorkspace,
} from "./sidebarWorkspaceFixture";

const inputs: AppModeInputs = {
  task_mode: "t2va",
  user_intent: "Synthetic managed lifecycle intent.",
  duration_milliseconds: 8_000,
  frame_count: 192,
};
const ownedGraph = Object.freeze({
  nodeIds: Object.freeze(["1", "8", "45"]),
  linkIds: Object.freeze(["15", "17", "18"]),
  anchorNodeId: "6",
  fingerprint: `sha256:${"b".repeat(64)}`,
});

function links(value: unknown): string[] {
  if (Array.isArray(value)) {
    if (
      value.length === 2 &&
      (typeof value[0] === "string" || typeof value[0] === "number") &&
      typeof value[1] === "number"
    )
      return [String(value[0])];
    return value.flatMap(links);
  }
  if (value !== null && typeof value === "object")
    return Object.values(value as Record<string, unknown>).flatMap(links);
  return [];
}

function withOfficialLinkedDimensions(
  output: Record<string, unknown>,
  selectorInputs: Record<string, unknown> = {
    aspect_ratio: "16:9 (Widescreen)",
    megapixels: 0.4,
    multiple: 32,
  },
): Record<string, unknown> {
  const anchor = output["6"] as { inputs: Record<string, unknown> };
  anchor.inputs.width = ["47", 0];
  anchor.inputs.height = ["47", 1];
  output["47"] = {
    class_type: "ResolutionSelector",
    inputs: selectorInputs,
  };
  return output;
}

describe("M23-15 managed App Mode preparation", () => {
  it("freezes one full observation and derives a fresh mutable content-free ProductShell bootstrap", () => {
    const compiled: AppModeCompiledPrompt = {
      output: withOfficialLinkedDimensions(createQualifiedBasePrompt(inputs)),
      workflow: {},
    };
    const prepared = buildManagedAppModePreparation(
      compiled,
      inputs,
      "existing",
      "sha256:" + "a".repeat(64),
      ownedGraph,
    );

    const bootstrap = prepared.bootstrap.output;
    const entries = Object.entries(bootstrap);
    expect(
      entries.filter(
        ([, node]) =>
          (node as { class_type?: unknown }).class_type ===
          "comfyui_h3_context.H3Context.ProductShell",
      ),
    ).toHaveLength(1);
    expect(
      entries.some(([, node]) =>
        [
          "MiniMaxH3ImageToVideo",
          "MiniMaxH3ReferenceToVideo",
          "SaveVideo",
        ].includes(String((node as { class_type?: unknown }).class_type)),
      ),
    ).toBe(false);
    const ids = new Set(Object.keys(bootstrap));
    expect(
      entries.flatMap(([, node]) => links(node)).every((id) => ids.has(id)),
    ).toBe(true);
    expect(prepared.bootstrap.workflow).toEqual({
      last_node_id: 0,
      last_link_id: 0,
      nodes: [],
      links: [],
      groups: [],
      config: {},
      extra: {},
      version: 0.4,
    });
    const workflow = prepared.bootstrap.workflow as Record<string, unknown> & {
      nodes: unknown[];
      links: unknown[];
      groups: unknown[];
    };
    expect(Object.isExtensible(workflow)).toBe(true);
    expect(Object.isFrozen(workflow.nodes)).toBe(false);
    expect(Object.isFrozen(workflow.links)).toBe(false);
    expect(Object.isFrozen(workflow.groups)).toBe(false);
    workflow.widget_idx_map = {};
    workflow.seed_widgets = {};
    const second = buildManagedAppModePreparation(
      compiled,
      inputs,
      "existing",
      "sha256:" + "a".repeat(64),
      ownedGraph,
    );
    expect(second.bootstrap.workflow).not.toBe(workflow);
    expect(second.bootstrap.workflow).not.toHaveProperty("widget_idx_map");
    expect(second.bootstrap.workflow).not.toHaveProperty("seed_widgets");
    expect(JSON.stringify(prepared.bootstrap.workflow)).not.toContain(
      "MiniMaxH3ImageToVideo",
    );
    expect(Object.isFrozen(prepared.observation)).toBe(true);
    expect(prepared.observation).toMatchObject({
      schema: "h3.context.prepared_graph_observation.v4",
      route: "existing",
      graph_fingerprint: "sha256:" + "a".repeat(64),
      owned_projection_fingerprint: ownedGraph.fingerprint,
      owned_node_ids: ownedGraph.nodeIds,
      owned_link_ids: ownedGraph.linkIds,
      expected_frames: 192,
      source_identity: null,
      fingerprint_domain: "output_producing_graph",
    });
    expect(prepared.observation.model_fingerprint).toMatch(
      /^sha256:[0-9a-f]{64}$/,
    );
    expect(prepared.observation.runtime_fingerprint).toMatch(
      /^sha256:[0-9a-f]{64}$/,
    );
    expect(prepared.productShellNodeId).toBe("8");
    expect(prepared.nativeAnchorNodeId).toBe("6");
    expect(prepared.observation).not.toHaveProperty("artifact_sink_node_id");
  });

  it("does not place predicted geometry or container facts in the observation", () => {
    const prepared = buildManagedAppModePreparation(
      { output: createQualifiedBasePrompt(inputs), workflow: {} },
      inputs,
      "existing",
      "sha256:" + "a".repeat(64),
      ownedGraph,
    );

    expect(prepared.observation.expected_frames).toBe(192);
    expect(prepared.observation.source_identity).toBeNull();
    expect(prepared.observation).not.toHaveProperty("expected_shape");
    expect(prepared.observation).not.toHaveProperty("expected_format");
    expect(prepared.observation).not.toHaveProperty("geometry_authority");
  });

  it("does not bind existing I2VA preparation to visible source identity", () => {
    const i2vaInputs: AppModeInputs = {
      ...inputs,
      task_mode: "i2va",
      first_frame_source: "node.source",
    };
    const output = createQualifiedBasePrompt(i2vaInputs, {
      first_frame: {
        class_type: "LoadImage",
        inputs: { image: "portrait/source.jpg" },
      },
    });
    const anchor = output["6"] as { inputs: Record<string, unknown> };
    anchor.inputs.width = ["48", 0];
    anchor.inputs.height = ["48", 1];
    output["47"] = {
      class_type: "ImageScaleToTotalPixels",
      inputs: {
        image: ["9", 0],
        upscale_method: "nearest-exact",
        megapixels: 0.8,
        resolution_steps: 32,
      },
    };
    output["48"] = {
      class_type: "GetImageSize",
      inputs: { image: ["47", 0] },
    };
    const compiled = { output, workflow: {} };
    const request = buildManagedSourceIdentityRequest(
      {
        nodes: [
          {
            id: "node.source",
            type: "LoadImage",
            widgets_values: ["portrait/source.jpg"],
          },
        ],
      },
      "node.source",
    );
    const receipt: InputGeometryReceipt = Object.freeze({
      schema: INPUT_GEOMETRY_RECEIPT_SCHEMA,
      receipt_handle: "ig_" + "r".repeat(40),
      source_fingerprint: "sha256:" + "b".repeat(64),
    });

    const prepared = buildManagedAppModePreparation(
      compiled,
      i2vaInputs,
      "existing",
      "sha256:" + "a".repeat(64),
      ownedGraph,
      receipt,
    );

    expect(request).toEqual({
      schema: "h3.context.input_geometry.request.v2",
      locator: "portrait/source.jpg",
    });
    expect(prepared.observation).toMatchObject({
      schema: "h3.context.prepared_graph_observation.v4",
      source_identity: null,
      expected_frames: 192,
    });
    expect(JSON.stringify(prepared.observation)).not.toContain(
      "portrait/source.jpg",
    );
  });

  it("reads no scale or size topology from the selected visible source", () => {
    const graph = {
      nodes: [
        {
          id: "node.source",
          type: "LoadImage",
          widgets_values: ["one/source.jpg", "irrelevant-preview-setting"],
        },
        {
          id: "scale.branch",
          type: "AnyScaleNode",
          widgets_values: [999, "arbitrary"],
        },
      ],
    };

    expect(buildManagedSourceIdentityRequest(graph, "node.source")).toEqual({
      schema: "h3.context.input_geometry.request.v2",
      locator: "one/source.jpg",
    });
  });

  it("fails when the selected visible LoadImage identity is missing or ambiguous", () => {
    expect(() =>
      buildManagedSourceIdentityRequest({ nodes: [] }, "node.source"),
    ).toThrow(expect.objectContaining({ code: "incompatible_graph" }));
  });

  it("does not inspect arbitrary anchor geometry or scale parameters", () => {
    const output = createQualifiedBasePrompt(inputs);
    const anchor = output["6"] as { inputs: Record<string, unknown> };
    anchor.inputs.width = ["47", 7];
    anchor.inputs.height = ["48", 9];
    anchor.inputs.length = ["49", 3];
    output["47"] = {
      class_type: "AnyScaleNode",
      inputs: { megapixels: 99, foreign: true },
    };
    output["48"] = {
      class_type: "AnySizeNode",
      inputs: { mode: "operator-owned" },
    };
    output["49"] = {
      class_type: "AnyLengthNode",
      inputs: { value: -1 },
    };

    const prepared = buildManagedAppModePreparation(
      { output, workflow: {} },
      inputs,
      "existing",
      "sha256:" + "a".repeat(64),
      ownedGraph,
    );

    expect(prepared.observation.expected_frames).toBe(192);
    expect(prepared.observation).not.toHaveProperty("expected_shape");
  });

  it("does not alias bootstrap nodes into the frozen full model prompt", () => {
    const compiled: AppModeCompiledPrompt = {
      output: createQualifiedBasePrompt(inputs),
      workflow: {},
    };
    const prepared = buildManagedAppModePreparation(
      compiled,
      inputs,
      "existing",
      "sha256:" + "a".repeat(64),
      ownedGraph,
    );
    const bootstrapRequest = prepared.bootstrap.output["1"] as {
      inputs: Record<string, unknown>;
    };
    const fullRequest = compiled.output["1"] as {
      inputs: Record<string, unknown>;
    };

    bootstrapRequest.inputs.user_intent = "host-mutated bootstrap";

    expect(fullRequest.inputs.user_intent).toBe(inputs.user_intent);
    expect(bootstrapRequest).not.toBe(fullRequest);
  });

  it("does not inspect SaveVideo count, format, or codec", () => {
    const output = createQualifiedBasePrompt(inputs);
    delete output["43"];
    output["44"] = {
      class_type: "AnyVideoSink",
      inputs: {
        format: "operator-owned",
        codec: { codec: "foreign" },
      },
    };

    const prepared = buildManagedAppModePreparation(
      { output, workflow: {} },
      inputs,
      "existing",
      "sha256:" + "a".repeat(64),
      ownedGraph,
    );

    expect(prepared.observation).not.toHaveProperty("expected_format");
    expect(prepared.observation).not.toHaveProperty("artifact_sink_node_id");
  });

  it("admits only the exact bounded stock SaveVideo SavedResult event", () => {
    expect(
      saveVideoArtifactFromExecuted({
        prompt_id: "prompt.model.1",
        node: "node.save.video",
        output: {
          images: [
            {
              filename: "managed_00001_.mp4",
              subfolder: "video/h3-context",
              type: "output",
            },
          ],
          animated: [true],
        },
      }),
    ).toEqual({
      promptId: "prompt.model.1",
      outputNodeId: "node.save.video",
      locator: {
        filename: "managed_00001_.mp4",
        subfolder: "video/h3-context",
        type: "output",
      },
    });
    expect(
      saveVideoArtifactFromExecuted({
        prompt_id: "prompt.model.1",
        node: "node.save.video",
        output: {
          images: [
            { filename: "one.mp4", subfolder: "video", type: "output" },
            { filename: "two.mp4", subfolder: "video", type: "output" },
          ],
          animated: [true],
        },
      }),
    ).toBeUndefined();
  });
});

function coordinatorResult(
  disposition: string,
  state: "running" | "succeeded",
  stateFingerprintMarker?: string,
): SequenceCoordinatorResult {
  const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
  const wire = managedCoordinatorResponse(
    disposition,
    wires[state],
    wires.production[state],
  );
  if (stateFingerprintMarker !== undefined) {
    const stateFingerprint = `sha256:${stateFingerprintMarker.repeat(64)}`;
    (wire.sequence as Record<string, unknown>).state_fingerprint =
      stateFingerprint;
    const production = wire.production as {
      generation_sequence: Record<string, unknown>;
    };
    production.generation_sequence.state_fingerprint = stateFingerprint;
  }
  return decodeSequenceCoordinatorResult(wire);
}

function memberCoordinatorResult(
  disposition: string,
  state: "running" | "succeeded",
): SequenceCoordinatorResult {
  const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
  const wire = managedCoordinatorResponse(
    disposition,
    wires[state],
    wires.production[state],
  );
  delete wire.production;
  wire.schema = "h3.context.generation_coordinator.managed_member_response.v1";
  wire.production_member_authority = {
    schema: "h3.context.generation_coordinator.production_member_authority.v1",
    workspace_handle: wires.production[state].workspace_handle,
    workspace_id: wires.production[state].workspace_id,
    member_segment_id: "segment_1",
  };
  return decodeSequenceCoordinatorResult(wire);
}

function managedCorrelationHarness(
  responses: readonly (SequenceCoordinatorResult | Error)[],
) {
  const session = createShellSession();
  const renderCurrent = vi.fn();
  const completeAppModeLifecycle = vi.fn();
  const failAppModeLifecycle = vi.fn();
  const workflow = {};
  const productionSend = vi.fn();
  const productionDestinations = createProductionDestinationStore();
  const send = vi.fn(
    async (
      _requestId: string,
      _action: SequenceCoordinatorAction,
      _payload: Record<string, unknown>,
    ) => {
      const next = responses[send.mock.calls.length - 1];
      if (next === undefined) throw new Error("unexpected coordinator call");
      if (next instanceof Error) throw next;
      return next;
    },
  );
  const managed = {
    run: 7,
    prepared: {
      observation: {
        route: "existing",
        graph_fingerprint: `sha256:${"1".repeat(64)}`,
        owned_projection_fingerprint: `sha256:${"3".repeat(64)}`,
      },
      productShellNodeId: "17",
      managedIdentity: {
        workflowAuthority: {},
        ownedReference: {
          nodeIds: [],
          linkIds: [],
          anchorNodeId: "17",
          authoredWidgetNodeIds: [],
        },
      },
    },
    bootstrap: {
      projection: structuredClone(validProductShell),
      workspace: structuredClone(validSidebarWorkspace),
    },
    authority: coordinatorResult("running", "running"),
    modelPromptId: "prompt.model.1",
    pendingProjectionPromptIds: new Set<string>(),
    pendingTerminals: new Map(),
    pendingArtifacts: new Map(),
    artifactRetryInFlight: false,
    transition: Promise.resolve(),
    completed: false,
  } as unknown as ActiveManagedRun;
  session.activeManagedRun = managed;
  session.state = {
    status: "working",
    phase: "generating",
    transactionId: managed.run,
    existingGraph: true,
    graphFingerprint: managed.prepared.observation.graph_fingerprint,
  };
  const actions = {
    browserRequestSessionToken: "test-session",
    isCurrentAppModeRun: (run: number) => run === managed.run,
    writeProductionSessionHandle: vi.fn(),
    currentProductionProjection: () =>
      "projection" in session.productionState
        ? session.productionState.projection
        : undefined,
    renderCurrent,
    nleSyncOwnerRenewal: vi.fn(),
    completeAppModeLifecycle,
    failAppModeLifecycle,
  };
  const deps = {
    sequenceCoordinator: { send },
    productionActions: { send: productionSend },
    productionDestinations,
    appModeController: { activeWorkflow: () => workflow },
    pageRegistry: { register: vi.fn() },
    appModeLifecycle: { invalidate: vi.fn() },
    productionProposalDispatcher: {
      advanceProjection: vi.fn(),
      bind: vi.fn(),
      captureCurrent: vi.fn(),
      clearCurrentSource: vi.fn(),
      observe: vi.fn(),
      refreshProjection: vi.fn(),
      resetAll: vi.fn(),
    },
  };
  const correlation = createAppModeCorrelation({
    session,
    actions,
    deps,
  } as unknown as ShellRuntime);
  return {
    session,
    managed,
    send,
    renderCurrent,
    completeAppModeLifecycle,
    failAppModeLifecycle,
    workflow,
    productionSend,
    productionDestinations,
    correlation,
  };
}

describe("M23-32 managed terminal truthfulness", () => {
  it("rejects a compact child before changing legacy session authority", () => {
    const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
    const wire = managedCoordinatorResponse(
      "current",
      wires.running,
      wires.production.running,
    );
    delete wire.production;
    wire.schema = "h3.context.generation_coordinator.managed_child_response.v1";
    wire.production_authority = {
      schema: "h3.context.generation_coordinator.production_authority.v1",
      workspace_handle: wires.production.running.workspace_handle,
      workspace_id: wires.running.workspace_id,
      workspace_revision: wires.running.workspace_revision,
      workspace_fingerprint: wires.running.workspace_fingerprint,
      automatic_plan_fingerprint: `sha256:${"7".repeat(64)}`,
    };
    const compact = decodeSequenceCoordinatorResult(wire);
    const harness = managedCorrelationHarness([]);
    const authority = harness.managed.authority;
    const sequence = harness.session.acceptedGenerationSequence;
    const production = harness.session.productionState;
    const handle = harness.session.productionSessionHandle;
    let failure: unknown;
    try {
      harness.correlation.applyManagedCoordinatorAuthority(
        harness.managed,
        compact,
      );
    } catch (error) {
      failure = error;
    }
    expect(harness.managed.authority).toBe(authority);
    expect(harness.session.acceptedGenerationSequence).toBe(sequence);
    expect(harness.session.productionState).toBe(production);
    expect(harness.session.productionSessionHandle).toBe(handle);
    expect(harness.renderCurrent).not.toHaveBeenCalled();
    expect(failure).toMatchObject({ code: "invalid_response", status: 500 });
  });

  it("accepts the exact accumulating member authority without installing a legacy sequence", () => {
    const current = memberCoordinatorResult("running", "running");
    if (
      current.schema !==
      "h3.context.generation_coordinator.managed_member_response.v1"
    )
      throw new Error("member fixture rejected");
    const harness = managedCorrelationHarness([]);
    const priorProduction = harness.session.productionState;
    harness.session.acceptedGenerationSequence = undefined;
    harness.managed.projectMember = {
      admission: {
        requestId: "production.admit.test.7",
        destination: { kind: "new", ordinal: 1 },
        target: null,
        workflow: {},
      },
      member: current.productionMemberAuthority,
      projection: decodeProductionWorkbenchProjection(
        managedLifecycleWires(`sha256:${"1".repeat(64)}`).production.running,
      ),
    };

    harness.correlation.applyManagedCoordinatorAuthority(
      harness.managed,
      current,
    );

    expect(harness.managed.authority).toBe(current);
    expect(harness.session.acceptedGenerationSequence).toBeUndefined();
    expect(harness.session.productionState).toBe(priorProduction);
  });

  it("refreshes the captured accumulating project before completing a member", async () => {
    const running = memberCoordinatorResult("running", "running");
    const succeeded = memberCoordinatorResult("succeeded", "succeeded");
    if (
      running.schema !==
        "h3.context.generation_coordinator.managed_member_response.v1" ||
      succeeded.schema !==
        "h3.context.generation_coordinator.managed_member_response.v1"
    )
      throw new Error("member fixture rejected");
    const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
    const runningProject = decodeProductionWorkbenchProjection(
      wires.production.running,
    );
    const succeededProject = decodeProductionWorkbenchProjection(
      wires.production.succeeded,
    );
    const harness = managedCorrelationHarness([]);
    harness.managed.authority = running;
    harness.managed.projectMember = {
      admission: {
        requestId: "production.admit.test.7",
        destination: { kind: "new", ordinal: 1 },
        target: null,
        workflow: harness.workflow,
      },
      member: running.productionMemberAuthority,
      projection: runningProject,
    };
    harness.session.productionState = {
      status: "ready",
      projection: runningProject,
    };
    harness.session.productionSessionHandle = runningProject.workspaceHandle;
    harness.session.acceptedGenerationSequence = undefined;
    harness.session.productionContextBinding = {
      productionWorkspaceHandle: runningProject.workspaceHandle,
      productionWorkspaceId: runningProject.workspaceId,
      contextWorkspaceHandle: "context.original",
    };
    harness.session.productionAccumulationNotice = undefined;
    harness.productionDestinations.bindProject(
      harness.workflow,
      runningProject,
    );
    harness.productionSend.mockResolvedValue({
      status: 200,
      projection: succeededProject,
    });

    await harness.correlation.settleManagedDisposition(
      harness.managed,
      succeeded,
    );

    expect(harness.productionSend).toHaveBeenCalledWith(
      expect.stringContaining("production.read_member"),
      "read_projection",
      { workspaceHandle: runningProject.workspaceHandle },
      undefined,
    );
    expect(harness.session.productionState).toEqual({
      status: "ready",
      projection: succeededProject,
    });
    expect(harness.session.productionAccumulationNotice).toEqual({
      kind: "added",
      projectOrdinal: 1,
      segmentOrdinal: 1,
      viewUpdated: true,
    });
    expect(harness.session.activeManagedRun).toBeUndefined();
    expect(harness.completeAppModeLifecycle).toHaveBeenCalledOnce();
  });

  it("retains the exact failed member as the only Retry target", async () => {
    const failed = memberCoordinatorResult("failed", "running");
    if (
      failed.schema !==
      "h3.context.generation_coordinator.managed_member_response.v1"
    )
      throw new Error("member fixture rejected");
    const wires = managedLifecycleWires(`sha256:${"1".repeat(64)}`);
    const project = decodeProductionWorkbenchProjection(
      wires.production.running,
    );
    const harness = managedCorrelationHarness([]);
    harness.managed.authority = failed;
    harness.managed.projectMember = {
      admission: {
        requestId: "production.admit.test.7",
        destination: {
          kind: "project",
          ordinal: 3,
          workspaceHandle: project.workspaceHandle,
          workspaceId: project.workspaceId,
        },
        target: {
          workspaceHandle: project.workspaceHandle,
          workspaceId: project.workspaceId,
          segmentId: null,
        },
        workflow: harness.workflow,
      },
      member: failed.productionMemberAuthority,
      projection: project,
    };
    harness.session.productionState = { status: "ready", projection: project };
    harness.productionDestinations.bindProject(harness.workflow, project);
    harness.productionSend.mockResolvedValue({
      status: 200,
      projection: project,
    });

    await harness.correlation.settleManagedDisposition(harness.managed, failed);

    expect(harness.session.retryableManagedProductionMember).toEqual({
      workflow: harness.workflow,
      workspaceHandle: project.workspaceHandle,
      workspaceId: project.workspaceId,
      segmentId: "segment_1",
    });
    expect(harness.session.state).toMatchObject({
      status: "error",
      code: "execution_failed",
      recovery: "retry",
    });
  });

  it("records exact success without an artifact and waits visibly for output", async () => {
    const pending = coordinatorResult("verification_pending", "running");
    const harness = managedCorrelationHarness([pending]);

    expect(
      harness.correlation.consumeManagedTerminal({
        promptId: "prompt.model.1",
        kind: "success",
      }),
    ).toBe(true);
    await harness.managed.transition;

    expect(harness.send).toHaveBeenCalledOnce();
    expect(harness.send.mock.calls[0]?.[1]).toBe("close_managed_run");
    expect(harness.send.mock.calls[0]?.[2]).toMatchObject({
      queue_prompt_id: "prompt.model.1",
      kind: "success",
      artifact: null,
    });
    expect(harness.session.state).toMatchObject({
      status: "working",
      phase: "verifying_output",
    });
    expect(harness.session.activeManagedRun).toBe(harness.managed);
    expect(harness.completeAppModeLifecycle).not.toHaveBeenCalled();
  });

  it("uses backend verification-pending authority after reconnect", async () => {
    const pending = coordinatorResult("verification_pending", "running");
    const harness = managedCorrelationHarness([pending]);
    const prior = harness.session.state;
    if (prior.status === "host_unavailable")
      throw new Error("nested host loss");
    harness.session.state = {
      status: "host_unavailable",
      phase: "lost",
      priorStatus: prior.status,
      prior,
    };

    await harness.correlation.reconcileAfterHostReconnect();

    expect(harness.send.mock.calls[0]?.[1]).toBe("read_managed_run");
    expect(harness.session.state).toMatchObject({
      status: "working",
      phase: "verifying_output",
    });
    expect(harness.session.activeManagedRun).toBe(harness.managed);
    expect(harness.completeAppModeLifecycle).not.toHaveBeenCalled();
  });

  it.each(["terminal_first", "artifact_first"] as const)(
    "completes once from verified output when events arrive $0",
    async (order) => {
      const pending = coordinatorResult("verification_pending", "running", "8");
      const succeeded = coordinatorResult("succeeded", "succeeded");
      const harness = managedCorrelationHarness(
        order === "terminal_first" ? [pending, succeeded] : [succeeded],
      );
      const terminal = {
        promptId: "prompt.model.1",
        kind: "success" as const,
      };
      const artifact = {
        promptId: "prompt.model.1",
        outputNodeId: "node.save.video",
        locator: {
          filename: "managed_00001_.mp4",
          subfolder: "video/h3-context",
          type: "output" as const,
        },
      };

      if (order === "terminal_first") {
        harness.correlation.consumeManagedTerminal(terminal);
        await harness.managed.transition;
        expect(harness.session.state).toMatchObject({
          status: "working",
          phase: "verifying_output",
        });
        expect(harness.completeAppModeLifecycle).not.toHaveBeenCalled();
        harness.correlation.consumeManagedArtifact(artifact);
      } else {
        harness.correlation.consumeManagedArtifact(artifact);
        await harness.managed.transition;
        expect(harness.send).not.toHaveBeenCalled();
        harness.correlation.consumeManagedTerminal(terminal);
      }
      await harness.managed.transition;

      const finalClose = harness.send.mock.calls.at(-1);
      expect(finalClose?.[1]).toBe("close_managed_run");
      expect(finalClose?.[2]).toMatchObject({
        expected_state_fingerprint:
          order === "terminal_first"
            ? `sha256:${"8".repeat(64)}`
            : `sha256:${"d".repeat(64)}`,
        queue_prompt_id: "prompt.model.1",
        kind: "success",
        artifact: {
          output_node_id: "node.save.video",
          locator: artifact.locator,
        },
      });
      expect(harness.completeAppModeLifecycle).toHaveBeenCalledOnce();
      expect(harness.session.activeManagedRun).toBeUndefined();
      expect(harness.session.state.status).toBe("projected");
    },
  );

  it("ignores duplicate and conflicting terminals after exact success is recorded", async () => {
    const harness = managedCorrelationHarness([
      coordinatorResult("verification_pending", "running"),
    ]);
    harness.correlation.consumeManagedTerminal({
      promptId: "prompt.model.1",
      kind: "success",
    });
    await harness.managed.transition;

    for (const kind of ["success", "error", "interrupted"] as const) {
      harness.correlation.consumeManagedTerminal({
        promptId: "prompt.model.1",
        kind,
      });
      await harness.managed.transition;
    }

    expect(harness.send).toHaveBeenCalledOnce();
    expect(harness.managed.pendingTerminals.get("prompt.model.1")).toBe(
      "success",
    );
    expect(harness.session.state).toMatchObject({
      status: "working",
      phase: "verifying_output",
    });
    expect(harness.failAppModeLifecycle).not.toHaveBeenCalled();
  });

  it.each([
    ["error", "failed", "execution_failed"],
    ["interrupted", "interrupted", "execution_interrupted"],
  ] as const)(
    "keeps a buffered artifact out of a %s close",
    async (kind, disposition, expectedCode) => {
      const harness = managedCorrelationHarness([
        coordinatorResult(disposition, "running"),
      ]);
      harness.correlation.consumeManagedArtifact({
        promptId: "prompt.model.1",
        outputNodeId: "node.save.video",
        locator: {
          filename: "managed_00001_.mp4",
          subfolder: "video/h3-context",
          type: "output",
        },
      });
      await harness.managed.transition;
      harness.correlation.consumeManagedTerminal({
        promptId: "prompt.model.1",
        kind,
      });
      await harness.managed.transition;

      expect(harness.send.mock.calls[0]?.[2]).toMatchObject({
        kind,
        artifact: null,
      });
      expect(harness.session.state).toMatchObject({
        status: "error",
        code: expectedCode,
        recovery: "retry",
      });
      expect(harness.failAppModeLifecycle).toHaveBeenCalledWith(
        7,
        expectedCode,
        "retry",
      );
    },
  );

  it("reconciles a lost or stale null-close response without requeueing", async () => {
    const pending = coordinatorResult("verification_pending", "running", "8");
    const harness = managedCorrelationHarness([
      new Error("synthetic stale state fingerprint"),
      pending,
    ]);

    harness.correlation.consumeManagedTerminal({
      promptId: "prompt.model.1",
      kind: "success",
    });
    await harness.managed.transition;

    expect(harness.send.mock.calls.map((call) => call[1])).toEqual([
      "close_managed_run",
      "read_managed_run",
    ]);
    expect(harness.session.state).toMatchObject({
      status: "working",
      phase: "verifying_output",
    });
    expect(harness.managed.authority.sequence.state_fingerprint).toBe(
      `sha256:${"8".repeat(64)}`,
    );
    expect(harness.failAppModeLifecycle).not.toHaveBeenCalled();
  });

  it("refuses ambiguous ownership when readback has no recorded terminal", async () => {
    const harness = managedCorrelationHarness([
      new Error("synthetic lost close response"),
      coordinatorResult("current", "running"),
    ]);

    harness.correlation.consumeManagedTerminal({
      promptId: "prompt.model.1",
      kind: "success",
    });
    await harness.managed.transition;

    expect(harness.send.mock.calls.map((call) => call[1])).toEqual([
      "close_managed_run",
      "read_managed_run",
    ]);
    expect(harness.session.state).toMatchObject({
      status: "error",
      code: "ambiguous_host_ownership",
      recovery: "use_native",
    });
    expect(harness.completeAppModeLifecycle).not.toHaveBeenCalled();
  });

  it("restores late-artifact correlation from a reconnect read", async () => {
    const pending = coordinatorResult("verification_pending", "running", "8");
    const succeeded = coordinatorResult("succeeded", "succeeded");
    const harness = managedCorrelationHarness([pending, succeeded]);
    harness.managed.pendingTerminals.clear();
    const prior = harness.session.state;
    if (prior.status === "host_unavailable")
      throw new Error("nested host loss");
    harness.session.state = {
      status: "host_unavailable",
      phase: "lost",
      priorStatus: prior.status,
      prior,
    };

    await harness.correlation.reconcileAfterHostReconnect();
    harness.correlation.consumeManagedArtifact({
      promptId: "prompt.model.1",
      outputNodeId: "node.save.video",
      locator: {
        filename: "managed_00001_.mp4",
        subfolder: "video/h3-context",
        type: "output",
      },
    });
    await harness.managed.transition;

    expect(harness.send.mock.calls.map((call) => call[1])).toEqual([
      "read_managed_run",
      "close_managed_run",
    ]);
    expect(harness.send.mock.calls[1]?.[2]).toMatchObject({
      expected_state_fingerprint: `sha256:${"8".repeat(64)}`,
      kind: "success",
      artifact: expect.objectContaining({
        output_node_id: "node.save.video",
      }),
    });
    expect(harness.completeAppModeLifecycle).toHaveBeenCalledOnce();
  });

  it("does not claim a foreign terminal or artifact", async () => {
    const harness = managedCorrelationHarness([]);

    expect(
      harness.correlation.consumeManagedTerminal({
        promptId: "prompt.foreign",
        kind: "success",
      }),
    ).toBe(false);
    expect(
      harness.correlation.consumeManagedArtifact({
        promptId: "prompt.foreign",
        outputNodeId: "node.save.video",
        locator: {
          filename: "foreign.mp4",
          subfolder: "video",
          type: "output",
        },
      }),
    ).toBe(false);
    await harness.managed.transition;

    expect(harness.send).not.toHaveBeenCalled();
    expect(harness.session.state).toMatchObject({
      status: "working",
      phase: "generating",
    });
  });
});
