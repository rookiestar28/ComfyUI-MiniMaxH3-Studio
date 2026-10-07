import { describe, expect, it, vi } from "vitest";

import {
  canonicalStringFingerprint,
  sha256Text,
} from "../src/contracts/canonicalFingerprint";
import {
  PRODUCTION_CANONICAL_LOWERING_SCHEMA,
  graphFingerprint,
} from "../src/host/appMode";
import {
  MANAGED_SEQUENCE_ROUTE,
  ManagedSequenceClientError,
  type EligibleSegmentExecution,
} from "../src/host/managedSequenceClient";
import {
  observeOwnedGraph,
  type OwnedGraphReference,
} from "../src/host/ownedGraphIdentity";
import {
  MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA,
  MANAGED_PREPARED_CONTEXT_SCHEMA,
  createProductionManagedChildResolver,
  decodeProductionManagedPreparedContext,
} from "../src/host/productionManagedChildResolver";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";

const fp = (digit: string) => `sha256:${digit.repeat(64)}`;
const prompt = "A blue sphere turns slowly.";
const profile = (name: "h3_base" | "h3_full_reference") => ({
  name,
  version: "1.0",
});
const profileFingerprint = (name: "h3_base" | "h3_full_reference") =>
  sha256Text(JSON.stringify(profile(name)));
const contextWorkspaceHandle = `ws_${"c".repeat(40)}`;
const workflowAuthority = {};
const ownedReference = Object.freeze({
  nodeIds: Object.freeze(["1", "2"]),
  linkIds: Object.freeze(["10"]),
  anchorNodeId: "2",
  authoredWidgetNodeIds: Object.freeze(["1"]),
});

function graph(intent = "previous prompt") {
  return {
    last_node_id: 2,
    last_link_id: 10,
    nodes: [
      {
        id: 1,
        type: "H3ContextRequest",
        mode: 0,
        widgets_values: [intent],
        inputs: [],
        outputs: [{ name: "request", type: "H3_CONTEXT_REQUEST" }],
      },
      {
        id: 2,
        type: "MiniMaxH3ImageToVideo",
        mode: 0,
        inputs: [{ name: "prompt", type: "STRING", link: 10 }],
        outputs: [],
      },
    ],
    links: [[10, 1, 0, 2, 0, "STRING"]],
    groups: [],
    config: {},
    extra: {},
    version: 0.4,
  };
}

function graphWithForeignNoise() {
  const value = graph();
  return {
    ...value,
    nodes: [
      ...value.nodes.map((node, index) => ({
        ...node,
        pos: [100 + index * 40, 200 + index * 20],
        size: [320, 180],
      })),
      {
        id: 99,
        type: "ForeignHostNode",
        mode: 0,
        widgets_values: ["foreign presentation"],
        inputs: [],
        outputs: [],
        pos: [900, 700],
        size: [120, 80],
      },
    ],
    config: { foreign_layout_revision: 7 },
    extra: { host_metadata: { zoom: 1.25, selected: [99] } },
  };
}

function execution(): EligibleSegmentExecution {
  return Object.freeze({
    parentSequenceId: "managed.sequence.1",
    parentAuthorizationFingerprint: fp("1"),
    segmentId: "segment.1",
    slotRevision: 4,
    attemptEpoch: 1,
    materializationReceiptFingerprint: fp("2"),
    predecessorTerminalFingerprint: null,
    requestId: "prepare.segment.1",
    jobCount: 1,
    fingerprint: fp("3"),
  });
}

type ReferenceWire = {
  asset_id: string;
  kind: "image" | "video" | "audio";
  role: string;
  connection_order: number;
  metadata: null;
  paired_video_id: string | null;
};

function reference(
  assetId: string,
  kind: ReferenceWire["kind"],
  role: string,
  order: number,
  pairedVideoId: string | null = null,
): ReferenceWire {
  return {
    asset_id: assetId,
    kind,
    role,
    connection_order: order,
    metadata: null,
    paired_video_id: pairedVideoId,
  };
}

function preparedContext(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const selectedProfile = profile("h3_base");
  return {
    schema: MANAGED_PREPARED_CONTEXT_SCHEMA,
    parent_sequence_id: execution().parentSequenceId,
    parent_revision: execution().slotRevision,
    authorization_fingerprint: execution().parentAuthorizationFingerprint,
    segment_id: execution().segmentId,
    eligible_execution_fingerprint: execution().fingerprint,
    materialization_receipt_fingerprint:
      execution().materializationReceiptFingerprint,
    context_workspace_handle: contextWorkspaceHandle,
    context_report_id: "report.segment.1",
    context_report_revision: 1,
    context_report_fingerprint: fp("4"),
    canonical_prompt: prompt,
    canonical_prompt_fingerprint: canonicalStringFingerprint(prompt),
    task_mode: "t2va",
    duration_milliseconds: 8_000,
    frame_count: 192,
    profile: selectedProfile,
    profile_fingerprint: profileFingerprint("h3_base"),
    ordered_references: [],
    reference_registry_fingerprint: fp("5"),
    native_binding_fingerprint: fp("6"),
    canonical_lowering: {
      schema: PRODUCTION_CANONICAL_LOWERING_SCHEMA,
      base_report_fingerprint: fp("7"),
      base_report_revision: 0,
      override_revision: 1,
      reason: "Materialize approved Production segment prompt",
    },
    ...overrides,
  };
}

function environment(responseValue = preparedContext()) {
  let visible: unknown = graph();
  const loadGraphData = vi.fn();
  const fetchApi = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => structuredClone(responseValue),
  }));
  const bound = fixtureBackedAppModeHost(
    {
      graph: { serialize: vi.fn(() => structuredClone(visible)) },
      extensionManager: {
        workflow: {
          activeWorkflow: workflowAuthority,
          openWorkflows: [workflowAuthority],
        },
      },
      loadGraphData,
    },
    {},
  );
  const currentOwnedGraphReference = vi.fn(() => ownedReference);
  return {
    app: bound.app,
    fetchApi,
    loadGraphData,
    currentOwnedGraphReference,
    resolver: createProductionManagedChildResolver({
      app: bound.app,
      fetchApi,
      currentOwnedGraphReference,
    }),
    setVisible(next: unknown) {
      visible = next;
    },
  };
}

function deferredEnvironment() {
  let release!: (value: unknown) => void;
  const response = new Promise<unknown>((resolve) => {
    release = resolve;
  });
  let visible: unknown = graph();
  let referenceAuthority: OwnedGraphReference = ownedReference;
  const workflowStore = {
    activeWorkflow: workflowAuthority as object,
    openWorkflows: [workflowAuthority] as object[],
  };
  const loadGraphData = vi.fn();
  const fetchApi = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => response,
  }));
  const bound = fixtureBackedAppModeHost(
    {
      graph: { serialize: () => structuredClone(visible) },
      extensionManager: { workflow: workflowStore },
      loadGraphData,
    },
    {},
  );
  const resolver = createProductionManagedChildResolver({
    app: bound.app,
    fetchApi,
    currentOwnedGraphReference: () => referenceAuthority,
  });
  return {
    app: bound.app,
    loadGraphData,
    resolver,
    release,
    setVisible(value: unknown) {
      visible = value;
    },
    setReference(value: OwnedGraphReference) {
      referenceAuthority = value;
    },
    workflowStore,
  };
}

describe("Production managed child resolver", () => {
  it("keeps parent identity across legitimate child writes while refreshing owned currentness", async () => {
    const wire = preparedContext();
    const subject = environment(wire);
    const first = await subject.resolver(
      execution(),
      contextWorkspaceHandle,
      0,
    );
    const nextGraph = { ...graph(prompt), extra: { foreign_revision: 2 } };
    subject.setVisible(nextGraph);
    const next = {
      ...execution(),
      segmentId: "segment.2",
      slotRevision: 8,
      fingerprint: fp("4"),
    };
    Object.assign(wire, {
      segment_id: next.segmentId,
      parent_revision: next.slotRevision,
      eligible_execution_fingerprint: next.fingerprint,
    });
    const second = await subject.resolver(next, contextWorkspaceHandle, 1);
    expect(second.activeWorkflowFingerprint).toBe(
      first.activeWorkflowFingerprint,
    );
    expect(second.previousOwnedProjectionFingerprint).toBe(
      observeOwnedGraph(nextGraph, ownedReference).fingerprint,
    );
    expect(second.previousOwnedProjectionFingerprint).not.toBe(
      first.previousOwnedProjectionFingerprint,
    );
    expect(subject.loadGraphData).not.toHaveBeenCalled();
  });

  it("binds the first dirty child after reused predecessors without requiring index zero", async () => {
    const subject = environment();
    const result = await subject.resolver(
      execution(),
      contextWorkspaceHandle,
      1,
    );
    expect(result.workflowAuthority).toBe(workflowAuthority);
    expect(result.activeWorkflowFingerprint).toBe(graphFingerprint(graph()));
    expect(subject.fetchApi).toHaveBeenCalledOnce();
    expect(subject.loadGraphData).not.toHaveBeenCalled();
  });

  it("refuses identical-content workflow replacement between children", async () => {
    const subject = deferredEnvironment();
    subject.release(preparedContext());
    await subject.resolver(execution(), contextWorkspaceHandle, 0);
    const replacement = {};
    subject.workflowStore.activeWorkflow = replacement;
    subject.workflowStore.openWorkflows = [replacement];
    await expect(
      subject.resolver(execution(), contextWorkspaceHandle, 1),
    ).rejects.toMatchObject({ code: "workflow_identity_changed" });
  });

  it("binds a new parent at its first child without carrying the old graph label", async () => {
    const wire = preparedContext();
    const subject = environment(wire);
    const first = await subject.resolver(
      execution(),
      contextWorkspaceHandle,
      0,
    );
    const nextGraph = graph(prompt);
    subject.setVisible(nextGraph);
    const next = { ...execution(), parentSequenceId: "managed.sequence.2" };
    Object.assign(wire, { parent_sequence_id: next.parentSequenceId });
    const second = await subject.resolver(next, contextWorkspaceHandle, 0);
    expect(second.activeWorkflowFingerprint).toBe(graphFingerprint(nextGraph));
    expect(second.activeWorkflowFingerprint).not.toBe(
      first.activeWorkflowFingerprint,
    );
  });

  it("reads one exact prepared authority and captures the real workflow and owned projection", async () => {
    const subject = environment();
    const result = await subject.resolver(
      execution(),
      contextWorkspaceHandle,
      0,
    );

    expect(result.inputs).toEqual({
      task_mode: "t2va",
      user_intent: prompt,
      duration_milliseconds: 8_000,
      frame_count: 192,
    });
    expect(result.options).toEqual({
      replaceExisting: true,
      artifactScope: `managed.${execution().fingerprint.slice(-32)}`,
    });
    expect(result.canonicalLowering).toEqual({
      schema: PRODUCTION_CANONICAL_LOWERING_SCHEMA,
      canonicalPrompt: prompt,
      baseReportFingerprint: fp("7"),
      baseReportRevision: 0,
      overrideRevision: 1,
      reason: "Materialize approved Production segment prompt",
    });
    expect(result.workflowAuthority).toBe(workflowAuthority);
    expect(result.activeWorkflowFingerprint).toBe(graphFingerprint(graph()));
    expect(result.previousOwnedProjectionFingerprint).toBe(
      observeOwnedGraph(graph(), ownedReference).fingerprint,
    );
    expect(Object.isFrozen(result)).toBe(true);
    expect(Object.isFrozen(result.inputs)).toBe(true);
    expect(subject.fetchApi).toHaveBeenCalledOnce();
    const [path, init] = subject.fetchApi.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(path).toBe(MANAGED_SEQUENCE_ROUTE);
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
    });
    expect(JSON.parse(String(init.body))).toEqual({
      schema: MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA,
      request_id: `read.prepared.${execution().fingerprint.slice(-40)}`,
      action: "read_prepared_child_context",
      payload: {
        parent_sequence_id: execution().parentSequenceId,
        expected_revision: execution().slotRevision,
        authorization_fingerprint: execution().parentAuthorizationFingerprint,
        segment_id: execution().segmentId,
        eligible_execution_fingerprint: execution().fingerprint,
        materialization_receipt_fingerprint:
          execution().materializationReceiptFingerprint,
        context_workspace_handle: contextWorkspaceHandle,
      },
    });
    expect(subject.loadGraphData).not.toHaveBeenCalled();
  });

  it.each([
    {
      mode: "i2va" as const,
      refs: [reference("image_1", "image", "first_frame", 1)],
    },
    {
      mode: "l2va" as const,
      refs: [reference("last_image_1", "image", "last_frame", 1)],
    },
    {
      mode: "fl2va" as const,
      refs: [
        reference("image_1", "image", "first_frame", 1),
        reference("last_image_1", "image", "last_frame", 2),
      ],
    },
  ])(
    "preserves exact $mode semantic references but refuses an unresolved host binding",
    async ({ mode, refs }) => {
      const wire = preparedContext({
        task_mode: mode,
        ordered_references: refs,
      });
      const decoded = decodeProductionManagedPreparedContext(wire);
      expect(decoded.task_mode).toBe(mode);
      expect(decoded.ordered_references).toEqual(refs);
      const subject = environment(wire);
      await expect(
        subject.resolver(execution(), contextWorkspaceHandle, 0),
      ).rejects.toMatchObject({
        code: "unsupported_prepared_context",
        status: 422,
      });
      expect(subject.loadGraphData).not.toHaveBeenCalled();
    },
  );

  it("preserves ordered Ref2VA media but does not reinterpret semantic asset IDs as host node IDs", async () => {
    const references = [
      reference("image_1", "image", "reference", 1),
      reference("video_1_audio", "audio", "audio_source", 2, "video_1"),
      reference("video_1", "video", "reference", 3),
      reference("audio_1", "audio", "audio_source", 4),
    ];
    const wire = preparedContext({
      task_mode: "ref2va",
      profile: profile("h3_full_reference"),
      profile_fingerprint: profileFingerprint("h3_full_reference"),
      ordered_references: references,
    });
    expect(
      decodeProductionManagedPreparedContext(wire).ordered_references,
    ).toEqual(references);
    const subject = environment(wire);
    await expect(
      subject.resolver(execution(), contextWorkspaceHandle, 0),
    ).rejects.toMatchObject({
      code: "unsupported_prepared_context",
      status: 422,
    });
    expect(subject.loadGraphData).not.toHaveBeenCalled();
  });

  it.each([
    ["parent_sequence_id", "managed.foreign"],
    ["parent_revision", 5],
    ["authorization_fingerprint", fp("a")],
    ["segment_id", "segment.foreign"],
    ["eligible_execution_fingerprint", fp("b")],
    ["materialization_receipt_fingerprint", fp("c")],
    ["context_workspace_handle", `ws_${"d".repeat(40)}`],
  ])("rejects a cross-authority %s response join", async (field, value) => {
    const subject = environment(preparedContext({ [field]: value }));
    await expect(
      subject.resolver(execution(), contextWorkspaceHandle, 0),
    ).rejects.toMatchObject({
      code: "cross_prepared_context_authority",
      status: 500,
    });
    expect(subject.loadGraphData).not.toHaveBeenCalled();
  });

  it("rejects unknown fields, content drift, profile drift and invalid reference authority", () => {
    const invalid = [
      preparedContext({ injected_report: {} }),
      preparedContext({ canonical_prompt_fingerprint: fp("f") }),
      preparedContext({
        profile: profile("h3_full_reference"),
        profile_fingerprint: profileFingerprint("h3_base"),
      }),
      preparedContext({
        ordered_references: [
          reference("audio_pair_1", "audio", "audio_source", 1, "video_1"),
          reference("image_1", "image", "reference", 2),
          reference("video_1", "video", "reference", 3),
        ],
      }),
      preparedContext({
        ordered_references: [reference("11", "image", "first_frame", 2)],
      }),
      preparedContext({
        ordered_references: [
          reference("31", "video", "reference", 1),
          reference("11", "image", "reference", 2),
        ],
      }),
      preparedContext({
        canonical_lowering: {
          schema: PRODUCTION_CANONICAL_LOWERING_SCHEMA,
          base_report_fingerprint: fp("7"),
          base_report_revision: 0,
          override_revision: 1,
          reason: "Materialize approved Production segment prompt",
          injected: true,
        },
      }),
      preparedContext({
        canonical_lowering: {
          schema: PRODUCTION_CANONICAL_LOWERING_SCHEMA,
          base_report_fingerprint: fp("7"),
          base_report_revision: 0,
          override_revision: 1,
          reason: "trust a different prompt",
        },
      }),
    ];
    for (const wire of invalid)
      expect(() => decodeProductionManagedPreparedContext(wire)).toThrow(
        ManagedSequenceClientError,
      );
  });

  it.each([
    preparedContext({
      task_mode: "t2va",
      ordered_references: [reference("image_1", "image", "first_frame", 1)],
    }),
    preparedContext({
      task_mode: "ref2va",
      profile: profile("h3_full_reference"),
      profile_fingerprint: profileFingerprint("h3_full_reference"),
      ordered_references: [
        reference("image_1", "image", "subject_reference", 1),
      ],
    }),
    preparedContext({
      task_mode: "ref2va",
      profile: profile("h3_full_reference"),
      profile_fingerprint: profileFingerprint("h3_full_reference"),
      ordered_references: [
        reference("image_1", "image", "reference", 1),
        reference("image_2", "image", "reference", 2),
      ],
    }),
    preparedContext({
      task_mode: "fl2va",
      ordered_references: [
        reference("last_image_1", "image", "last_frame", 1),
        reference("image_1", "image", "first_frame", 2),
      ],
    }),
    preparedContext({ canonical_lowering: null }),
    preparedContext({
      canonical_lowering: {
        schema: PRODUCTION_CANONICAL_LOWERING_SCHEMA,
        base_report_fingerprint: fp("7"),
        base_report_revision: 1,
        override_revision: 1,
        reason: "Materialize approved Production segment prompt",
      },
    }),
    (() => {
      const oversizedPrompt = "x".repeat(4_097);
      return preparedContext({
        canonical_prompt: oversizedPrompt,
        canonical_prompt_fingerprint:
          canonicalStringFingerprint(oversizedPrompt),
      });
    })(),
  ])(
    "refuses a valid backend context App Mode cannot represent without semantic loss",
    async (wire) => {
      const subject = environment(wire);
      await expect(
        subject.resolver(execution(), contextWorkspaceHandle, 0),
      ).rejects.toMatchObject({
        code: "unsupported_prepared_context",
        status: 422,
      });
    },
  );

  it("rejects when an owned authored node changes during the prepared context read", async () => {
    let release!: (value: unknown) => void;
    const response = new Promise<unknown>((resolve) => {
      release = resolve;
    });
    let visible = graph();
    const loadGraphData = vi.fn();
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => response,
    }));
    const bound = fixtureBackedAppModeHost(
      {
        graph: { serialize: () => structuredClone(visible) },
        extensionManager: {
          workflow: {
            activeWorkflow: workflowAuthority,
            openWorkflows: [workflowAuthority],
          },
        },
        loadGraphData,
      },
      {},
    );
    const resolver = createProductionManagedChildResolver({
      app: bound.app,
      fetchApi,
      currentOwnedGraphReference: () => ownedReference,
    });
    const pending = resolver(execution(), contextWorkspaceHandle, 0);
    visible = graph("foreign edit");
    release(preparedContext());

    await expect(pending).rejects.toMatchObject({
      code: "workflow_identity_changed",
      status: 409,
    });
    expect(loadGraphData).not.toHaveBeenCalled();
  });

  it("accepts foreign nodes, host metadata and layout changes while owned projection stays current", async () => {
    const subject = deferredEnvironment();
    const pending = subject.resolver(execution(), contextWorkspaceHandle, 0);
    const latest = graphWithForeignNoise();
    subject.setVisible(latest);
    subject.release(preparedContext());

    const result = await pending;
    expect(result.activeWorkflowFingerprint).toBe(graphFingerprint(latest));
    expect(result.activeWorkflowFingerprint).not.toBe(
      graphFingerprint(graph()),
    );
    expect(result.previousOwnedProjectionFingerprint).toBe(
      observeOwnedGraph(graph(), ownedReference).fingerprint,
    );
    expect(observeOwnedGraph(latest, ownedReference).fingerprint).toBe(
      result.previousOwnedProjectionFingerprint,
    );
    expect(subject.loadGraphData).not.toHaveBeenCalled();
  });

  it("rejects an anchor prompt-binding change during the prepared context read", async () => {
    const subject = deferredEnvironment();
    const pending = subject.resolver(execution(), contextWorkspaceHandle, 0);
    const value = graph();
    subject.setVisible({
      ...value,
      nodes: value.nodes.map((node) =>
        node.id === 2
          ? {
              ...node,
              inputs: [{ name: "prompt", type: "STRING", link: null }],
            }
          : node,
      ),
    });
    subject.release(preparedContext());

    await expect(pending).rejects.toMatchObject({
      code: "workflow_identity_changed",
      status: 409,
    });
    expect(subject.loadGraphData).not.toHaveBeenCalled();
  });

  it.each(["active workflow", "open tab count"])(
    "rejects a changed %s during the prepared context read",
    async (change) => {
      const subject = deferredEnvironment();
      const pending = subject.resolver(execution(), contextWorkspaceHandle, 0);
      if (change === "active workflow") {
        const replacement = {};
        subject.workflowStore.activeWorkflow = replacement;
        subject.workflowStore.openWorkflows = [replacement];
      } else {
        subject.workflowStore.openWorkflows.push({});
      }
      subject.release(preparedContext());

      await expect(pending).rejects.toMatchObject({
        code: "workflow_identity_changed",
        status: 409,
      });
      expect(subject.loadGraphData).not.toHaveBeenCalled();
    },
  );

  it("rejects when the owned graph reference changes while the prepared context read is pending", async () => {
    let release!: (value: unknown) => void;
    const response = new Promise<unknown>((resolve) => {
      release = resolve;
    });
    let referenceAuthority = ownedReference;
    const loadGraphData = vi.fn();
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => response,
    }));
    const bound = fixtureBackedAppModeHost(
      {
        graph: { serialize: () => graph() },
        extensionManager: {
          workflow: {
            activeWorkflow: workflowAuthority,
            openWorkflows: [workflowAuthority],
          },
        },
        loadGraphData,
      },
      {},
    );
    const resolver = createProductionManagedChildResolver({
      app: bound.app,
      fetchApi,
      currentOwnedGraphReference: () => referenceAuthority,
    });
    const pending = resolver(execution(), contextWorkspaceHandle, 0);
    referenceAuthority = Object.freeze({
      ...ownedReference,
      nodeIds: Object.freeze(["1"]),
    });
    release(preparedContext());

    await expect(pending).rejects.toMatchObject({
      code: "workflow_identity_changed",
      status: 409,
    });
    expect(loadGraphData).not.toHaveBeenCalled();
  });

  it("closes malformed canonical text and decoder inputs as typed response errors", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    for (const value of [
      cyclic,
      preparedContext({ canonical_prompt: "unsafe\ud800text" }),
    ]) {
      expect(() => decodeProductionManagedPreparedContext(value)).toThrow(
        ManagedSequenceClientError,
      );
    }
  });

  it("accepts an exact positive Decimal metadata value without binary64 underflow", () => {
    const value = preparedContext({
      task_mode: "ref2va",
      profile: profile("h3_full_reference"),
      profile_fingerprint: profileFingerprint("h3_full_reference"),
      ordered_references: [
        {
          ...reference("11", "image", "reference", 1),
          metadata: {
            duration_seconds: `0.${"0".repeat(400)}1`,
            width: null,
            height: null,
            frame_count: null,
            sample_rate: null,
            channels: null,
          },
        },
      ],
    });
    expect(
      decodeProductionManagedPreparedContext(value).ordered_references,
    ).toHaveLength(1);
  });

  it("does not parse a refusal body, mutate the canvas, or consult a queue seam", async () => {
    const json = vi.fn();
    const loadGraphData = vi.fn();
    const queuePrompt = vi.fn();
    const fetchApi = vi.fn(async () => ({
      ok: false,
      status: 409,
      json,
    }));
    const bound = fixtureBackedAppModeHost(
      {
        graph: { serialize: () => graph() },
        extensionManager: {
          workflow: {
            activeWorkflow: workflowAuthority,
            openWorkflows: [workflowAuthority],
          },
        },
        loadGraphData,
      },
      { queuePrompt },
    );
    const resolver = createProductionManagedChildResolver({
      app: bound.app,
      fetchApi,
      currentOwnedGraphReference: () => ownedReference,
    });

    await expect(
      resolver(execution(), contextWorkspaceHandle, 0),
    ).rejects.toMatchObject({
      code: "prepared_context_rejected",
      status: 409,
    });
    expect(json).not.toHaveBeenCalled();
    expect(loadGraphData).not.toHaveBeenCalled();
    expect(queuePrompt).not.toHaveBeenCalled();
  });
});
