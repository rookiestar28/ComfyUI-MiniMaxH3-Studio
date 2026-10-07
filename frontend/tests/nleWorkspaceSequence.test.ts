// M25-16 planning → readiness → managed serial sequence inside the workspace session: every
// step is an explicit action bound to exact identities, a material edit invalidates admission
// and approval, readiness is requested explicitly, the start is refused without exact-plan
// qualification, B1 detach/reattach/resume map runner dispositions to typed UI states, and the
// bounded ETag poll stops with the overlay.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { observeOwnedGraph } from "../src/host/ownedGraphIdentity";
import {
  NLE_SEQUENCE_POLL_INTERVAL_MS,
  createNleWorkspaceSession,
} from "../src/lifecycle/nleWorkspaceSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";

import {
  fp,
  HANDLE,
  CONTEXT_HANDLE,
  productionProjection,
  planningProjection,
  proposedProjection,
  plan,
  readyReadiness,
} from "./support/nleSequenceFixture";

type Snapshot = {
  attached: boolean;
  parentSequenceId: string | null;
  parentState: string | null;
  parentRevision: number;
  activeSegmentId: string | null;
  activeQueuePromptId: string | null;
  failure: string | null;
};

function subject() {
  const session = createShellSession();
  session.container = document.createElement("div");
  session.productionState = {
    status: "ready",
    projection: productionProjection(),
  };
  session.workspaceState = {
    status: "ready",
    projection: {
      workspace_id: CONTEXT_HANDLE,
      report_revision: 0,
      report_fingerprint: fp("a"),
    },
  } as unknown as typeof session.workspaceState;
  const snapshot: Snapshot = {
    attached: false,
    parentSequenceId: null,
    parentState: null,
    parentRevision: 0,
    activeSegmentId: null,
    activeQueuePromptId: null,
    failure: null,
  };
  let pointer:
    { parentSequenceId: string; readAuthorityFingerprint: string } | undefined;
  const planningSend = vi.fn(async () => planningProjection());
  const readinessSend = vi.fn(async () => readyReadiness());
  const sequenceRead = vi.fn(async () => ({
    status: 304 as const,
    etag: '"e1"',
    projection: null,
  }));
  const runProductionIntent = vi.fn(
    async (_intent: { action: string }) => undefined,
  );
  const actions = {
    renderCurrent: vi.fn(),
    selectPage: vi.fn(),
    mediaRuntimeLeaveContext: vi.fn(),
    runProductionIntent,
    startNewProductionProject: vi.fn(),
    managedSerialSequenceSnapshot: vi.fn(() => ({ ...snapshot })),
    startManagedSerialSequence: vi.fn(async () => {
      Object.assign(snapshot, {
        attached: true,
        parentSequenceId: "parent.1",
        parentState: "running",
        activeSegmentId: "segment.one",
        activeQueuePromptId: "prompt.1",
      });
      pointer = {
        parentSequenceId: "parent.1",
        readAuthorityFingerprint: fp("f"),
      };
    }),
    detachManagedSerialSequence: vi.fn(async () => {
      Object.assign(snapshot, {
        attached: false,
        parentState: "paused_client_absent",
      });
    }),
    reattachManagedSerialSequence: vi.fn(async () => ({
      disposition: "resumable",
      parentState: "paused_client_absent",
    })),
    resumeManagedSerialSequence: vi.fn(async () => {
      Object.assign(snapshot, { attached: true, parentState: "running" });
    }),
    cancelManagedSerialSequence: vi.fn(async () => {
      Object.assign(snapshot, { attached: false, parentState: "cancelled" });
    }),
    retryManagedSerialSequence: vi.fn(async () => undefined),
    managedSerialCanvasLabel: vi.fn(
      (): {
        workflowAuthority: object;
        activeWorkflowFingerprint: string;
      } | null => null,
    ),
  };
  const runtime = {
    session,
    deps: {
      app: { graph: null },
      api: { fetchApi: vi.fn() },
      productionPlanningClient: { send: planningSend },
      managedQualificationClient: { send: readinessSend },
      managedSequenceReattachStore: { read: vi.fn(() => pointer) },
      managedSequenceClient: { read: sequenceRead },
      authoringOutputCapabilityClient: { read: vi.fn() },
    },
    actions,
  } as unknown as ShellRuntime;
  const nle = createNleWorkspaceSession(runtime);
  const expand = () => {
    session.nleWorkspace = Object.freeze({
      ...session.nleWorkspace,
      surface: Object.freeze({
        ...session.nleWorkspace.surface,
        status: "expanded" as const,
        generation: 1,
      }),
    });
  };
  return {
    nle,
    session,
    deps: runtime.deps as unknown as Record<string, unknown>,
    actions,
    planningSend,
    readinessSend,
    sequenceRead,
    runProductionIntent,
    snapshot,
    expand,
    setPointer: (value: typeof pointer) => {
      pointer = value;
    },
  };
}

async function planThroughImport(
  ctx: ReturnType<typeof subject>,
  options: { holds?: string[] } = {},
) {
  const holds = options.holds ?? ["managed_execution_qualification_pending"];
  ctx.planningSend
    .mockResolvedValueOnce(planningProjection())
    .mockResolvedValueOnce(
      planningProjection({ admission_id: "admission_owned" }),
    )
    .mockResolvedValueOnce(proposedProjection())
    .mockResolvedValueOnce(
      plan({
        start_hold_codes: holds,
        startable: holds.length === 0,
      }) as unknown as ReturnType<typeof planningProjection>,
    );
  await ctx.nle.nlePrepareContext();
  await ctx.nle.nleAdmitStoryboard("canonical_optimized_prompt");
  await ctx.nle.nlePropose();
  await ctx.nle.nleApproveAndImportPlan();
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("M25-16 planning inside the workspace", () => {
  it("binds prepare_context to the exact Context and Production identities", async () => {
    const ctx = subject();
    ctx.nle.nleSetTargetSeconds(20);
    ctx.nle.nleSetSegmentationPolicy("fixed_10");
    await ctx.nle.nlePrepareContext();
    expect(ctx.planningSend).toHaveBeenCalledTimes(1);
    const [requestId, action, payload, signal] = ctx.planningSend.mock
      .calls[0] as unknown as [
      string,
      string,
      Record<string, unknown>,
      AbortSignal,
    ];
    expect(requestId).toMatch(/^plan\.prepare\.[0-9a-f]{16}\.\d+$/u);
    expect(action).toBe("prepare_context");
    expect(payload).toEqual({
      workspace_handle: HANDLE,
      expected_workspace_revision: 2,
      expected_workspace_fingerprint: fp("a"),
      context_workspace_handle: CONTEXT_HANDLE,
      expected_report_revision: 0,
      expected_report_fingerprint: fp("a"),
      expected_planning_revision: 0,
      target_seconds: 20,
      policy: "fixed_10",
    });
    expect(signal).toBeInstanceOf(AbortSignal);
    const planning = ctx.session.nleWorkspace.planning;
    expect(planning.status).toBe("prepared");
    expect(planning.projection?.planning_revision).toBe(1);
    expect(planning.boundWorkspaceFingerprint).toBe(fp("a"));
  });

  it("clamps the target and treats a material edit as invalidating admission and approval", async () => {
    const ctx = subject();
    ctx.nle.nleSetTargetSeconds(999);
    expect(ctx.session.nleWorkspace.planning.targetSeconds).toBe(60);
    ctx.nle.nleSetTargetSeconds(1);
    expect(ctx.session.nleWorkspace.planning.targetSeconds).toBe(4);
    await planThroughImport(ctx);
    expect(ctx.session.nleWorkspace.planning.status).toBe("imported");
    expect(ctx.session.nleWorkspace.planning.plan?.plan_fingerprint).toBe(
      fp("b"),
    );
    ctx.nle.nleSetTargetSeconds(30);
    const planning = ctx.session.nleWorkspace.planning;
    expect(planning.status).toBe("idle");
    expect(planning.projection).toBeNull();
    expect(planning.plan).toBeNull();
    expect(planning.targetSeconds).toBe(30);
    expect(ctx.session.nleWorkspace.readiness.status).toBe("idle");
  });

  it("admits, proposes and imports with the projection selectors and causes no queue call", async () => {
    const ctx = subject();
    await planThroughImport(ctx);
    const actions = ctx.planningSend.mock.calls.map(
      (call) => (call as unknown[])[1],
    );
    expect(actions).toEqual([
      "prepare_context",
      "admit_storyboard",
      "propose",
      "import_plan",
    ]);
    const admit = (ctx.planningSend.mock.calls[1] as unknown[])[2] as Record<
      string,
      unknown
    >;
    expect(admit).toMatchObject({
      workspace_handle: HANDLE,
      source_kind: "canonical_optimized_prompt",
      typed_rows: [],
      user_reviewed: false,
    });
    const propose = (ctx.planningSend.mock.calls[2] as unknown[])[2] as Record<
      string,
      unknown
    >;
    expect(propose.admission_id).toBe("admission_owned");
    const importPlan = (
      ctx.planningSend.mock.calls[3] as unknown[]
    )[2] as Record<string, unknown>;
    expect(importPlan.proposal_id).toBe("proposal_owned");
    expect(ctx.runProductionIntent).toHaveBeenCalledWith({
      action: "read_projection",
    });
    expect(ctx.actions.startManagedSerialSequence).not.toHaveBeenCalled();
    expect(ctx.session.nleWorkspace.planning.status).toBe("imported");
  });

  it("refuses the reviewed-rows admission without rows and sends typed rows when present", async () => {
    const ctx = subject();
    await ctx.nle.nlePrepareContext();
    await ctx.nle.nleAdmitStoryboard("user_reviewed_typed_rows");
    expect(ctx.planningSend).toHaveBeenCalledTimes(1);
    ctx.nle.nleSetStoryboardRows([
      {
        shotId: "shot_1",
        ordinal: 1,
        startMilliseconds: 0,
        endMilliseconds: 10_000,
        text: "A blue sphere turns.",
        hardBoundary: false,
      },
    ]);
    ctx.planningSend.mockResolvedValueOnce(
      planningProjection({ admission_id: "admission_owned" }),
    );
    await ctx.nle.nleAdmitStoryboard("user_reviewed_typed_rows");
    const admit = (ctx.planningSend.mock.calls[1] as unknown[])[2] as Record<
      string,
      unknown
    >;
    expect(admit.user_reviewed).toBe(true);
    expect(admit.typed_rows).toHaveLength(1);
    expect((admit.typed_rows as Record<string, unknown>[])[0]).toMatchObject({
      schema: "h3.context.storyboard_shot.v1",
      shot_id: "shot_1",
      exact_dialogue: [],
      visible_text: [],
    });
  });

  it("does not admit or propose against a moved Production workspace", async () => {
    const ctx = subject();
    await ctx.nle.nlePrepareContext();
    ctx.session.productionState = {
      status: "ready",
      projection: productionProjection(fp("9")),
    };
    await ctx.nle.nleAdmitStoryboard("canonical_optimized_prompt");
    await ctx.nle.nlePropose();
    await ctx.nle.nleApproveAndImportPlan();
    expect(ctx.planningSend).toHaveBeenCalledTimes(1);
  });

  it("maps a refusal to a typed planning error without adopting", async () => {
    const ctx = subject();
    ctx.planningSend.mockRejectedValueOnce(
      Object.assign(new Error("x"), { status: 409 }),
    );
    await ctx.nle.nlePrepareContext();
    expect(ctx.session.nleWorkspace.planning.status).toBe("error");
    expect(ctx.session.nleWorkspace.planning.error).toBe(
      "planning_refused_409",
    );
    expect(ctx.session.nleWorkspace.planning.projection).toBeNull();
  });

  it("names a Context that cannot be planned instead of pointing at storyboard rows", async () => {
    const ctx = subject();
    ctx.planningSend.mockRejectedValueOnce(
      Object.assign(new Error("x"), { status: 422 }),
    );
    await ctx.nle.nlePrepareContext();
    const planning = ctx.session.nleWorkspace.planning;
    expect(planning.status).toBe("error");
    expect(planning.error).toBe("planning_source_unsupported");
    expect(planning.projection).toBeNull();
    expect(planning.storyboardReviewOpen).toBe(false);
  });

  it("leads a refused generated storyboard to the storyboard script", async () => {
    const ctx = subject();
    await ctx.nle.nlePrepareContext();
    ctx.planningSend.mockRejectedValueOnce(
      Object.assign(new Error("x"), { status: 422 }),
    );
    await ctx.nle.nleAdmitStoryboard("canonical_optimized_prompt");
    let planning = ctx.session.nleWorkspace.planning;
    expect(planning.status).toBe("error");
    expect(planning.error).toBe("planning_storyboard_unavailable");
    // The review region holds the script box, so it opens with the guidance.
    expect(planning.storyboardReviewOpen).toBe(true);
    expect(planning.projection?.admission_id ?? null).toBeNull();

    // Rows written from the script are the answer; the guidance does not outlive them.
    ctx.nle.nleSetStoryboardRows([
      {
        shotId: "shot-1",
        ordinal: 1,
        startMilliseconds: 0,
        endMilliseconds: 60_000,
        text: "The whole video.",
        hardBoundary: false,
      },
    ]);
    planning = ctx.session.nleWorkspace.planning;
    expect(planning.error).toBeNull();
    expect(planning.status).toBe("prepared");
    expect(planning.storyboardReviewOpen).toBe(true);
  });

  it("keeps the ordinary refusal for reviewed rows and for other statuses", async () => {
    const ctx = subject();
    await ctx.nle.nlePrepareContext();
    ctx.nle.nleSetStoryboardRows([
      {
        shotId: "shot-1",
        ordinal: 1,
        startMilliseconds: 0,
        endMilliseconds: 10_000,
        text: "Too short for the target.",
        hardBoundary: false,
      },
    ]);
    ctx.nle.nleOpenStoryboardReview(true);
    ctx.planningSend.mockRejectedValueOnce(
      Object.assign(new Error("x"), { status: 422 }),
    );
    await ctx.nle.nleAdmitStoryboard("user_reviewed_typed_rows");
    expect(ctx.session.nleWorkspace.planning.error).toBe(
      "planning_refused_422",
    );
    // An ordinary refusal survives a row edit: only the script guidance is spent by rows.
    ctx.nle.nleSetStoryboardRows([]);
    expect(ctx.session.nleWorkspace.planning.error).toBe(
      "planning_refused_422",
    );

    ctx.planningSend.mockRejectedValueOnce(
      Object.assign(new Error("x"), { status: 409 }),
    );
    await ctx.nle.nleAdmitStoryboard("canonical_optimized_prompt");
    expect(ctx.session.nleWorkspace.planning.error).toBe(
      "planning_refused_409",
    );
  });

  it("creates a new destination for a plan refused by a populated project", async () => {
    const ctx = subject();
    ctx.planningSend
      .mockResolvedValueOnce(planningProjection())
      .mockResolvedValueOnce(
        planningProjection({ admission_id: "admission_owned" }),
      )
      .mockResolvedValueOnce(proposedProjection())
      .mockRejectedValueOnce(Object.assign(new Error("x"), { status: 409 }));
    await ctx.nle.nlePrepareContext();
    await ctx.nle.nleAdmitStoryboard("canonical_optimized_prompt");
    await ctx.nle.nlePropose();
    await ctx.nle.nleApproveAndImportPlan();
    expect(ctx.session.nleWorkspace.planning.error).toBe(
      "planning_requires_empty_project",
    );

    ctx.runProductionIntent.mockImplementationOnce(async (intent) => {
      expect(intent).toEqual({ action: "create_workspace_from_context" });
      ctx.session.productionState = {
        status: "ready",
        projection: {
          ...productionProjection(),
          workspaceHandle: `pw_${"n".repeat(32)}`,
          workspaceId: "workspace.new",
          segments: [],
        },
      };
    });
    await ctx.nle.nleCreatePlannedProject();
    expect(ctx.actions.startNewProductionProject).toHaveBeenCalledOnce();
    expect(ctx.session.nleWorkspace.planning).toMatchObject({
      status: "idle",
      error: null,
    });
  });

  it("prepares again after the server has dropped an expired planning binding", async () => {
    // B-M1605-PLAN-01: the server forgets a planning binding after its TTL and then accepts
    // only a first prepare (revision 0), while the browser kept sending its last revision, so
    // every Prepare answered 409 until the target or policy was edited.
    const ctx = subject();
    await ctx.nle.nlePrepareContext();
    expect(
      ctx.session.nleWorkspace.planning.projection?.planning_revision,
    ).toBe(1);
    ctx.planningSend.mockImplementation((async (
      _requestId: string,
      _action: string,
      payload: Record<string, unknown>,
    ) => {
      if (payload.expected_planning_revision !== 0)
        throw Object.assign(new Error("x"), { status: 409 });
      return planningProjection();
    }) as never);
    await ctx.nle.nlePrepareContext();
    expect(ctx.session.nleWorkspace.planning.status).toBe("prepared");
    expect(ctx.session.nleWorkspace.planning.error).toBeNull();
    const revisions = ctx.planningSend.mock.calls.map(
      (call) =>
        ((call as unknown[])[2] as Record<string, unknown>)
          .expected_planning_revision,
    );
    expect(revisions).toEqual([0, 1, 0]);
  });

  it("does not repeat a refused prepare that already sent the first revision", async () => {
    const ctx = subject();
    ctx.planningSend.mockRejectedValue(
      Object.assign(new Error("x"), { status: 409 }),
    );
    await ctx.nle.nlePrepareContext();
    expect(ctx.planningSend).toHaveBeenCalledTimes(1);
    expect(ctx.session.nleWorkspace.planning.error).toBe(
      "planning_refused_409",
    );
  });
});

describe("M25-16 readiness and managed sequence inside the workspace", () => {
  it("requests readiness explicitly for the exact imported plan and gates the start on it", async () => {
    const ctx = subject();
    await planThroughImport(ctx);
    expect(ctx.nle.nleSequenceStartable()).toBe(false);
    expect(ctx.readinessSend).not.toHaveBeenCalled();
    await ctx.nle.nleRequestReadiness();
    expect(ctx.readinessSend).toHaveBeenCalledTimes(1);
    const [, action, selection] = ctx.readinessSend.mock
      .calls[0] as unknown as [string, string, Record<string, unknown>];
    expect(action).toBe("prepare_managed_readiness");
    expect(selection).toEqual({
      workspace_handle: HANDLE,
      expected_workspace_revision: 2,
      expected_workspace_fingerprint: fp("a"),
      expected_plan_fingerprint: fp("b"),
    });
    expect(ctx.session.nleWorkspace.readiness.status).toBe("ready");
    expect(ctx.session.nleWorkspace.readiness.boundPlanFingerprint).toBe(
      fp("b"),
    );
    expect(ctx.nle.nleSequenceStartable()).toBe(true);
  });

  it("starts the real pending-qualification plan once its exact readiness is ready", async () => {
    // B-M2522-START-01: every real plan carries managed_execution_qualification_pending and
    // startable=false; the exact-plan readiness is the authority that resolves that one hold.
    const ctx = subject();
    await planThroughImport(ctx);
    expect(ctx.session.nleWorkspace.planning.plan?.startable).toBe(false);
    expect(ctx.nle.nleSequenceStartable()).toBe(false);
    await ctx.nle.nleRequestReadiness();
    expect(ctx.nle.nleSequenceStartable()).toBe(true);
  });

  it("keeps any other start hold blocking even with ready readiness", async () => {
    for (const holds of [
      ["managed_execution_unsupported"],
      [
        "managed_execution_qualification_pending",
        "local_reference_unavailable",
      ],
    ]) {
      const ctx = subject();
      await planThroughImport(ctx, { holds });
      await ctx.nle.nleRequestReadiness();
      expect(ctx.session.nleWorkspace.readiness.status).toBe("ready");
      expect(ctx.nle.nleSequenceStartable(), holds.join(",")).toBe(false);
    }
  });

  it("reports a held or failed readiness as not startable", async () => {
    const ctx = subject();
    await planThroughImport(ctx);
    ctx.readinessSend.mockResolvedValueOnce({
      ...readyReadiness(),
      status: "held",
      reason: "host_capability_mismatch",
      qualification_fingerprint: null,
      qualification: null,
    });
    await ctx.nle.nleRequestReadiness();
    expect(ctx.session.nleWorkspace.readiness.status).toBe("held");
    expect(ctx.nle.nleSequenceStartable()).toBe(false);
    ctx.readinessSend.mockRejectedValueOnce(new Error("offline"));
    await ctx.nle.nleRequestReadiness();
    expect(ctx.session.nleWorkspace.readiness.status).toBe("error");
    expect(ctx.session.nleWorkspace.readiness.error).toBe(
      "managed_readiness_unavailable",
    );
  });

  it("starts exactly the qualified plan through the accepted runner and polls while expanded", async () => {
    const ctx = subject();
    ctx.expand();
    await planThroughImport(ctx);
    await ctx.nle.nleRequestReadiness();
    await ctx.nle.nleStartSequence();
    expect(ctx.actions.startManagedSerialSequence).toHaveBeenCalledTimes(1);
    const [intent, bindings] = (
      ctx.actions.startManagedSerialSequence as ReturnType<typeof vi.fn>
    ).mock.calls[0] as [Record<string, unknown>, Record<string, unknown>];
    expect(intent).toMatchObject({
      authorization: {
        workspace_handle: HANDLE,
        expected_plan_fingerprint: fp("b"),
        generation_plan_fingerprint: fp("6"),
        compiler_fingerprint: fp("e"),
        host_capability_fingerprint: fp("d"),
        explicit_intent: "generate_approved_sequence",
      },
      qualificationFingerprint: fp("c"),
      segmentIds: ["segment.one", "segment.two"],
    });
    expect(typeof bindings.resolveChild).toBe("function");
    const sequence = ctx.session.nleWorkspace.sequence;
    expect(sequence.ui).toBe("detach_ready");
    expect(sequence.parentSequenceId).toBe("parent.1");
    expect(sequence.busy).toBe(false);
    expect(sequence.polling).toBe(true);
    // A second start while attached is refused.
    await ctx.nle.nleStartSequence();
    expect(ctx.actions.startManagedSerialSequence).toHaveBeenCalledTimes(1);
    // The bounded poll reads the projection with its ETag and nothing else.
    await vi.advanceTimersByTimeAsync(NLE_SEQUENCE_POLL_INTERVAL_MS + 1);
    expect(ctx.sequenceRead).toHaveBeenCalledTimes(1);
    expect(ctx.sequenceRead.mock.calls[0]!.slice(0, 3)).toEqual([
      "parent.1",
      fp("f"),
      undefined,
    ]);
    ctx.sequenceRead.mockResolvedValueOnce({
      status: 200,
      etag: '"e2"',
      projection: {
        parentSequenceId: "parent.1",
        state: "running",
        activeSegmentId: "segment.two",
        revision: 3,
      },
    } as never);
    await vi.advanceTimersByTimeAsync(NLE_SEQUENCE_POLL_INTERVAL_MS + 1);
    expect(ctx.session.nleWorkspace.sequence.etag).toBe('"e2"');
    await vi.advanceTimersByTimeAsync(NLE_SEQUENCE_POLL_INTERVAL_MS + 1);
    expect((ctx.sequenceRead.mock.calls as unknown as unknown[][])[2]![2]).toBe(
      '"e2"',
    );
    // Closing the overlay stops the poll.
    ctx.nle.nleCloseOverlay("explicit_close");
    expect(ctx.session.nleWorkspace.sequence.polling).toBe(false);
    const reads = ctx.sequenceRead.mock.calls.length;
    await vi.advanceTimersByTimeAsync(NLE_SEQUENCE_POLL_INTERVAL_MS * 3);
    expect(ctx.sequenceRead).toHaveBeenCalledTimes(reads);
  });

  it("gives the child resolver the host fetchApi bound to its api object", async () => {
    // The host's api.fetchApi reads `this` (its user header), so a detached reference throws a
    // TypeError before any request is sent, and the first child fails as managed_sequence_failed.
    class HostApi {
      readonly receivers: unknown[] = [];
      async fetchApi(
        this: HostApi,
        _path: string,
        _init: RequestInit,
      ): Promise<Response> {
        this.receivers.push(this);
        return new Response(null, { status: 409 });
      }
    }
    const ctx = subject();
    ctx.expand();
    await planThroughImport(ctx);
    await ctx.nle.nleRequestReadiness();
    const api = new HostApi();
    const workflow = {};
    ctx.deps.api = api;
    ctx.deps.app = {
      extensionManager: {
        workflow: { activeWorkflow: workflow, openWorkflows: [workflow] },
      },
      graph: { serialize: () => ({ nodes: [], links: [] }) },
    };
    await ctx.nle.nleStartSequence();
    const [, bindings] = (
      ctx.actions.startManagedSerialSequence as ReturnType<typeof vi.fn>
    ).mock.calls[0] as [
      unknown,
      {
        resolveChild: (
          execution: unknown,
          context: string,
          index: number,
        ) => Promise<unknown>;
      },
    ];
    const execution = Object.freeze({
      parentSequenceId: "parent.1",
      parentAuthorizationFingerprint: fp("a"),
      segmentId: "segment.one",
      slotRevision: 2,
      attemptEpoch: 1,
      materializationReceiptFingerprint: fp("9"),
      predecessorTerminalFingerprint: null,
      requestId: "request.1",
      jobCount: 1,
      fingerprint: fp("8"),
    });
    await expect(
      bindings.resolveChild(execution, CONTEXT_HANDLE, 0),
    ).rejects.toMatchObject({
      name: "ManagedSequenceClientError",
      code: "prepared_context_rejected",
    });
    expect(api.receivers).toEqual([api]);
  });

  it("refuses a start whose readiness does not qualify the exact plan", async () => {
    const ctx = subject();
    await planThroughImport(ctx);
    ctx.readinessSend.mockResolvedValueOnce({
      ...readyReadiness(),
      qualification: {
        ...(readyReadiness().qualification as Record<string, unknown>),
        production_plan_fingerprint: fp("9"),
      },
    });
    await ctx.nle.nleRequestReadiness();
    await ctx.nle.nleStartSequence();
    expect(ctx.actions.startManagedSerialSequence).not.toHaveBeenCalled();
    expect(ctx.session.nleWorkspace.readiness.status).toBe("error");
    expect(ctx.session.nleWorkspace.readiness.error).toBe(
      "managed_start_unqualified",
    );
  });

  it("maps B1 detach, reattach, resume and cancel onto typed UI states", async () => {
    const ctx = subject();
    ctx.expand();
    await planThroughImport(ctx);
    await ctx.nle.nleRequestReadiness();
    await ctx.nle.nleStartSequence();
    expect(ctx.session.nleWorkspace.sequence.ui).toBe("detach_ready");
    await ctx.nle.nleDetachSequence();
    expect(ctx.actions.detachManagedSerialSequence).toHaveBeenCalledTimes(1);
    expect(ctx.session.nleWorkspace.sequence.ui).toBe("safe_to_leave");
    // Detach is idempotent from the UI: a second call does nothing.
    await ctx.nle.nleDetachSequence();
    expect(ctx.actions.detachManagedSerialSequence).toHaveBeenCalledTimes(1);
    expect(ctx.nle.nleRecoveryPointerPresent()).toBe(true);
    await ctx.nle.nleReattachSequence();
    expect(ctx.actions.reattachManagedSerialSequence).toHaveBeenCalledTimes(1);
    expect(ctx.session.nleWorkspace.sequence.ui).toBe("resume_ready");
    expect(ctx.session.nleWorkspace.sequence.reattach?.disposition).toBe(
      "resumable",
    );
    // Resume needs the runner's bound workflow label and a readable canvas; without them it is
    // recorded as unavailable rather than guessed.
    await ctx.nle.nleResumeSequence();
    expect(ctx.actions.resumeManagedSerialSequence).not.toHaveBeenCalled();
    expect(ctx.session.nleWorkspace.sequence.ui).toBe(
      "recovery_unavailable_or_expired",
    );
    expect(ctx.session.nleWorkspace.sequence.failure).toBe(
      "canvas_revalidation_unavailable",
    );
  });

  describe("canvas identity presented on resume and retry", () => {
    // After a child is written the canvas is its owned projection plus whatever the host
    // rewrote, so its whole-graph hash no longer equals the label the parent was bound with.
    const ownedReference = {
      nodeIds: ["7"],
      linkIds: [],
      anchorNodeId: "7",
      authoredWidgetNodeIds: ["7"],
    };
    const visible = {
      nodes: [{ id: 7, type: "H3Anchor", widgets_values: ["written"] }],
      links: [],
      extra: { ds: { scale: 1.7 } },
    };

    async function resumeReady(options: { switchedTab?: boolean } = {}) {
      const ctx = subject();
      ctx.expand();
      await planThroughImport(ctx);
      await ctx.nle.nleRequestReadiness();
      await ctx.nle.nleStartSequence();
      await ctx.nle.nleDetachSequence();
      await ctx.nle.nleReattachSequence();
      expect(ctx.session.nleWorkspace.sequence.ui).toBe("resume_ready");
      const bound = { path: "bound.json" };
      const other = { path: "other.json" };
      ctx.deps.app = {
        graph: { serialize: () => structuredClone(visible) },
        extensionManager: {
          workflow: {
            activeWorkflow: options.switchedTab ? other : bound,
            openWorkflows: [bound, other],
          },
        },
      };
      ctx.session.acceptedManagedIdentity = {
        ownedReference,
      } as unknown as typeof ctx.session.acceptedManagedIdentity;
      ctx.actions.managedSerialCanvasLabel.mockReturnValue({
        workflowAuthority: bound,
        activeWorkflowFingerprint: fp("1"),
      });
      return ctx;
    }

    it("resumes with the runner's bound workflow label and the owned projection observed now", async () => {
      const ctx = await resumeReady();
      await ctx.nle.nleResumeSequence();
      expect(ctx.actions.resumeManagedSerialSequence).toHaveBeenCalledWith({
        activeWorkflowFingerprint: fp("1"),
        ownedProjectionFingerprint: observeOwnedGraph(visible, ownedReference)
          .fingerprint,
      });
    });

    it("retries with the same bound label", async () => {
      const ctx = await resumeReady();
      await ctx.nle.nleRetrySegment("segment.one");
      expect(ctx.actions.retryManagedSerialSequence).toHaveBeenCalledWith({
        segmentId: "segment.one",
        activeWorkflowFingerprint: fp("1"),
        ownedProjectionFingerprint: observeOwnedGraph(visible, ownedReference)
          .fingerprint,
      });
    });

    it("clears a previous sequence error once an explicit retry or resume succeeds", async () => {
      // B-M1605-SEQ-01: the pane kept the last failure (`snapshot.failure ?? previous`) after the
      // runner cleared it, so a recovered sequence still showed its old error.
      for (const act of ["retry", "resume"] as const) {
        const ctx = await resumeReady();
        ctx.actions.resumeManagedSerialSequence.mockImplementationOnce(
          async () => {
            ctx.snapshot.failure = "queue_callback_incomplete";
            throw new Error("refused");
          },
        );
        await ctx.nle.nleResumeSequence();
        expect(ctx.session.nleWorkspace.sequence.failure, act).toBe(
          "queue_callback_incomplete",
        );
        Object.assign(ctx.snapshot, { failure: null });
        ctx.session.nleWorkspace = Object.freeze({
          ...ctx.session.nleWorkspace,
          sequence: Object.freeze({
            ...ctx.session.nleWorkspace.sequence,
            ui: "resume_ready" as const,
          }),
        });
        if (act === "retry") await ctx.nle.nleRetrySegment("segment.one");
        else await ctx.nle.nleResumeSequence();
        expect(ctx.session.nleWorkspace.sequence.failure, act).toBeNull();
      }
    });

    it("refuses without an accepted owned reference instead of sending an unmatched projection", async () => {
      const ctx = await resumeReady();
      ctx.session.acceptedManagedIdentity = undefined;
      await ctx.nle.nleResumeSequence();
      expect(ctx.actions.resumeManagedSerialSequence).not.toHaveBeenCalled();
      expect(ctx.session.nleWorkspace.sequence.failure).toBe(
        "canvas_revalidation_unavailable",
      );
    });

    it("refuses to present the label once another workflow tab is active", async () => {
      const ctx = await resumeReady({ switchedTab: true });
      await ctx.nle.nleResumeSequence();
      expect(ctx.actions.resumeManagedSerialSequence).not.toHaveBeenCalled();
      expect(ctx.session.nleWorkspace.sequence.failure).toBe(
        "canvas_revalidation_unavailable",
      );
    });
  });

  it.each([
    ["succeeded", "finished"],
    ["cancelled", "finished"],
    ["paused_failure", "failed"],
    ["paused_unknown_ownership", "paused_unknown_ownership"],
  ] as const)(
    "retains %s when the parent settles during detach",
    async (parentState, ui) => {
      const ctx = subject();
      ctx.expand();
      await planThroughImport(ctx);
      await ctx.nle.nleRequestReadiness();
      await ctx.nle.nleStartSequence();
      ctx.actions.detachManagedSerialSequence.mockImplementationOnce(
        async () => {
          Object.assign(ctx.snapshot, { attached: false, parentState });
        },
      );
      await ctx.nle.nleDetachSequence();
      expect(ctx.session.nleWorkspace.sequence.ui).toBe(ui);
      expect(ctx.session.nleWorkspace.sequence.busy).toBe(false);
    },
  );

  it("adopts a completed runner after its exact recovery pointer is cleared", async () => {
    const ctx = subject();
    ctx.expand();
    await planThroughImport(ctx);
    await ctx.nle.nleRequestReadiness();
    await ctx.nle.nleStartSequence();
    Object.assign(ctx.snapshot, { parentState: "succeeded", attached: false });
    ctx.setPointer(undefined);
    await ctx.nle.nleRefreshSequenceProjection();
    expect(ctx.session.nleWorkspace.sequence.ui).toBe("finished");
    expect(ctx.sequenceRead).not.toHaveBeenCalled();
  });

  it("reads the Production projection once when the sequence finishes, so assembly reflects it", async () => {
    const ctx = subject();
    ctx.expand();
    await planThroughImport(ctx);
    await ctx.nle.nleRequestReadiness();
    await ctx.nle.nleStartSequence();
    const readsBefore = ctx.runProductionIntent.mock.calls.length;
    Object.assign(ctx.snapshot, { parentState: "succeeded", attached: false });
    ctx.setPointer(undefined);
    await ctx.nle.nleRefreshSequenceProjection();
    expect(ctx.session.nleWorkspace.sequence.ui).toBe("finished");
    expect(ctx.runProductionIntent.mock.calls.slice(readsBefore)).toEqual([
      [{ action: "read_projection" }],
    ]);
    // Staying finished is not another transition.
    await ctx.nle.nleRefreshSequenceProjection();
    expect(ctx.runProductionIntent.mock.calls.length).toBe(readsBefore + 1);
  });

  it.each([
    ["waiting_for_current_child", "current_segment_still_owned"],
    ["artifact_authority_unavailable", "paused_unknown_ownership"],
    ["terminal_reconciled", "current_segment_completed"],
    ["expired", "recovery_unavailable_or_expired"],
  ] as const)("maps reattach disposition %s to %s", async (disposition, ui) => {
    const ctx = subject();
    ctx.setPointer({
      parentSequenceId: "parent.9",
      readAuthorityFingerprint: fp("f"),
    });
    (
      ctx.actions.reattachManagedSerialSequence as ReturnType<typeof vi.fn>
    ).mockResolvedValueOnce({
      disposition,
      parentState: "paused_client_absent",
    });
    await ctx.nle.nleReattachSequence();
    expect(ctx.session.nleWorkspace.sequence.ui).toBe(ui);
    expect(ctx.session.nleWorkspace.sequence.busy).toBe(false);
  });

  it("does not offer a new start while a failed sequence is still paused", async () => {
    // B-M1605-SEQ-02: a paused failed parent keeps its runner, so Start could only answer
    // already_started. Retry the failed segment or cancel the sequence first.
    const ctx = subject();
    ctx.expand();
    await planThroughImport(ctx);
    await ctx.nle.nleRequestReadiness();
    await ctx.nle.nleStartSequence();
    Object.assign(ctx.snapshot, {
      attached: false,
      parentState: "paused_failure",
    });
    await ctx.nle.nleRefreshSequenceProjection();
    ctx.setPointer(undefined);
    await ctx.nle.nleRefreshSequenceProjection();
    expect(ctx.session.nleWorkspace.sequence.ui).toBe("failed");
    expect(ctx.nle.nleSequenceStartable()).toBe(false);
    await ctx.nle.nleCancelSequence();
    expect(ctx.session.nleWorkspace.sequence.ui).toBe("finished");
    expect(ctx.nle.nleSequenceStartable()).toBe(true);
  });

  it("cancels through the runner and never while busy or idle", async () => {
    const ctx = subject();
    await ctx.nle.nleCancelSequence();
    expect(ctx.actions.cancelManagedSerialSequence).not.toHaveBeenCalled();
    ctx.expand();
    await planThroughImport(ctx);
    await ctx.nle.nleRequestReadiness();
    await ctx.nle.nleStartSequence();
    await ctx.nle.nleCancelSequence();
    expect(ctx.actions.cancelManagedSerialSequence).toHaveBeenCalledTimes(1);
    expect(ctx.session.nleWorkspace.sequence.ui).toBe("finished");
  });

  it("offers assembly actions only when Production allows them", async () => {
    const ctx = subject();
    await ctx.nle.nleAssembly("assemble_sequence");
    expect(ctx.runProductionIntent).not.toHaveBeenCalled();
  });
});
