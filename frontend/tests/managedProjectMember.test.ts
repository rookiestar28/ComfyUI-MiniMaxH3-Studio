import { describe, expect, it, vi } from "vitest";

import { decodeGenerationSequenceProjection } from "../src/contracts/generationSequenceCodec";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { decodeSemanticProposalReviewHandle } from "../src/contracts/semanticProposalReviewCodec";
import {
  admitManagedProjectDestination,
  buildMemberGenerationBinding,
  installManagedMemberProject,
  memberAuthorityMatchesDestination,
  readManagedMemberProject,
  reconcileManagedProposalProject,
  refreshManagedMemberProject,
  releaseManagedProjectDestination,
} from "../src/lifecycle/managedProjectMember";
import { createProductionDestinationStore } from "../src/host/productionDestination";
import { createProductionProposalDispatcher } from "../src/host/productionProposalDispatcher";
import type { ShellRuntime } from "../src/lifecycle/shellSession";
import { managedLifecycleWires } from "./support/managedLifecycleWire";

const graphFingerprint = `sha256:${"1".repeat(64)}`;
const wires = managedLifecycleWires(graphFingerprint);
const sequence = decodeGenerationSequenceProjection(wires.planned);
const production = decodeProductionWorkbenchProjection(
  wires.production.planned,
);
const member = Object.freeze({
  schema:
    "h3.context.generation_coordinator.production_member_authority.v1" as const,
  workspaceHandle: production.workspaceHandle,
  workspaceId: production.workspaceId,
  memberSegmentId: "segment_1",
});
const workspace = Object.freeze({
  reportId: "report-1",
  taskMode: "t2va" as const,
  requestedDurationMilliseconds: 8000,
  effectiveDurationMilliseconds: 8000,
  frameCount: 192,
  referenceIds: Object.freeze([] as string[]),
});
const request = Object.freeze({
  inputs: Object.freeze({
    task_mode: "t2va" as const,
    user_intent: "Synthetic member binding.",
    duration_milliseconds: 8000,
    frame_count: 192,
  }),
});

describe("M25-36 managed project member", () => {
  it("joins the compact member authority to the exact planned command", () => {
    expect(
      buildMemberGenerationBinding({
        production,
        member,
        sequence,
        workspace,
        request,
      }),
    ).toEqual({
      jobId: "job.1",
      sourceId: "report-1",
      referenceIds: [],
      requestedDurationMilliseconds: 8000,
      effectiveDurationMilliseconds: 8000,
      route: "new",
      inputs: request.inputs,
    });
  });

  it("rejects a cross-project authority and rewritten authored duration", () => {
    expect(
      buildMemberGenerationBinding({
        production,
        member: { ...member, workspaceId: "workspace_foreign" },
        sequence,
        workspace,
        request,
      }),
    ).toBeUndefined();
    expect(
      buildMemberGenerationBinding({
        production,
        member,
        sequence,
        workspace,
        request: {
          inputs: { ...request.inputs, duration_milliseconds: 7000 },
        },
      }),
    ).toBeUndefined();
  });

  it("matches create, append and exact regeneration destinations", () => {
    expect(memberAuthorityMatchesDestination(member, null)).toBe(true);
    expect(
      memberAuthorityMatchesDestination(member, {
        workspaceHandle: member.workspaceHandle,
        workspaceId: member.workspaceId,
        segmentId: null,
      }),
    ).toBe(true);
    expect(
      memberAuthorityMatchesDestination(member, {
        workspaceHandle: member.workspaceHandle,
        workspaceId: member.workspaceId,
        segmentId: member.memberSegmentId,
      }),
    ).toBe(true);
    expect(
      memberAuthorityMatchesDestination(member, {
        workspaceHandle: member.workspaceHandle,
        workspaceId: member.workspaceId,
        segmentId: "segment_foreign",
      }),
    ).toBe(false);
  });

  it("replays one lost admission with the identical request and releases once", async () => {
    const workflow = {};
    const destinations = createProductionDestinationStore();
    const sendDestination = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("lost response"))
      .mockResolvedValueOnce({ status: 202 })
      .mockResolvedValueOnce({ status: 204 });
    const client = { sendDestination };

    const admitted = await admitManagedProjectDestination({
      client,
      destinations,
      workflow,
      current: undefined,
      requestId: "production.admit.session.1",
    });

    expect(admitted).toMatchObject({
      requestId: "production.admit.session.1",
      destination: { kind: "new", ordinal: 1 },
      target: null,
    });
    expect(sendDestination).toHaveBeenCalledTimes(2);
    expect(sendDestination.mock.calls[1]).toEqual(
      sendDestination.mock.calls[0],
    );

    await releaseManagedProjectDestination({
      client,
      requestId: "production.release.session.1",
      admissionRequestId: admitted.requestId,
    });
    expect(sendDestination).toHaveBeenCalledTimes(3);
    expect(sendDestination.mock.calls[2]?.slice(0, 3)).toEqual([
      "production.release.session.1",
      "release_generation_destination",
      { admissionRequestId: admitted.requestId },
    ]);
  });

  it("admits an explicit failed member for regeneration instead of append", async () => {
    const workflow = {};
    const destinations = createProductionDestinationStore();
    destinations.bindProject(workflow, production);
    const sendDestination = vi.fn().mockResolvedValue({
      status: 200,
      projection: production,
    });

    const admitted = await admitManagedProjectDestination({
      client: { sendDestination },
      destinations,
      workflow,
      current: production,
      requestId: "production.admit.session.retry",
      segmentId: "segment_1",
    });

    expect(admitted.target).toEqual({
      workspaceHandle: production.workspaceHandle,
      workspaceId: production.workspaceId,
      segmentId: "segment_1",
    });
    expect(sendDestination.mock.calls[0]?.[2]).toEqual({
      target: admitted.target,
    });
  });

  it("refreshes only the exact captured project member", async () => {
    const send = vi
      .fn()
      .mockResolvedValue({ status: 200, projection: production });

    await expect(
      readManagedMemberProject({
        client: { send },
        requestId: "production.read_member.session.1",
        member,
      }),
    ).resolves.toBe(production);
    expect(send).toHaveBeenCalledWith(
      "production.read_member.session.1",
      "read_projection",
      { workspaceHandle: member.workspaceHandle },
      undefined,
    );

    await expect(
      readManagedMemberProject({
        client: {
          send: vi.fn().mockResolvedValue({
            status: 200,
            projection: decodeProductionWorkbenchProjection({
              ...wires.production.planned,
              workspace_handle: `pw_${"f".repeat(43)}`,
            }),
          }),
        },
        requestId: "production.read_member.session.2",
        member,
      }),
    ).rejects.toMatchObject({ code: "cross_authority_response" });
  });

  it("retains a manual source through a pending no-source member and its accepted refresh", () => {
    const dispatcher = createProductionProposalDispatcher({ send: vi.fn() });
    const proposalFingerprint = `sha256:${"d".repeat(64)}`;
    const proposalHandle = decodeSemanticProposalReviewHandle({
      schema: "h3.context.semantic_proposal_review_handle.v1",
      review_id: `review_${"r".repeat(32)}`,
      transaction_fingerprint: `sha256:${"b".repeat(64)}`,
      workspace_fingerprint: `sha256:${"c".repeat(64)}`,
      report_fingerprint: proposalFingerprint,
      correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
      available: true,
      reason: "review_available",
    });
    dispatcher.observe(
      {
        workspace_id: "context.workspace",
        report_revision: 1,
        report_fingerprint: proposalFingerprint,
      },
      proposalHandle,
    );
    const source = dispatcher.captureCurrent();
    expect(
      dispatcher.bind({ action: "create", source, after: production }),
    ).toBe(true);
    dispatcher.clearCurrentSource();
    const appendTarget = {
      workspaceHandle: production.workspaceHandle,
      workspaceId: production.workspaceId,
      segmentId: null,
    } as const;

    expect(
      reconcileManagedProposalProject(
        dispatcher,
        production,
        production,
        appendTarget,
      ),
    ).toBe(true);
    expect(dispatcher.rows(production)).toMatchObject([
      { segmentId: "segment_1", actionable: true },
    ]);

    const appended = Object.freeze({
      ...production,
      workspaceRevision: production.workspaceRevision + 1,
      workspaceFingerprint: `sha256:${"e".repeat(64)}`,
      segments: Object.freeze([
        ...production.segments,
        Object.freeze({
          ...production.segments[0]!,
          segmentId: "segment_2",
          ordinal: 2,
        }),
      ]),
      selectedSegmentIds: Object.freeze(["segment_1", "segment_2"]),
      runProgress: Object.freeze({ ...production.runProgress, total: 2 }),
    });
    expect(
      reconcileManagedProposalProject(
        dispatcher,
        production,
        appended,
        appendTarget,
      ),
    ).toBe(true);
    expect(dispatcher.rows(appended)).toMatchObject([
      { segmentId: "segment_1", actionable: true },
      {
        segmentId: "segment_2",
        actionable: false,
        reason: "not_provided",
      },
    ]);
  });

  it("keeps the current proposal owner when an old install loses its workflow destination", () => {
    const dispatcher = createProductionProposalDispatcher({ send: vi.fn() });
    const proposalFingerprint = `sha256:${"d".repeat(64)}`;
    dispatcher.observe(
      {
        workspace_id: "context.current",
        report_revision: 1,
        report_fingerprint: proposalFingerprint,
      },
      decodeSemanticProposalReviewHandle({
        schema: "h3.context.semantic_proposal_review_handle.v1",
        review_id: `review_${"s".repeat(32)}`,
        transaction_fingerprint: `sha256:${"b".repeat(64)}`,
        workspace_fingerprint: `sha256:${"c".repeat(64)}`,
        report_fingerprint: proposalFingerprint,
        correlation: { prompt_id: "prompt.current", execution_node_id: "17" },
        available: true,
        reason: "review_available",
      }),
    );
    const current = Object.freeze({
      ...production,
      workspaceHandle: `pw_${"z".repeat(43)}`,
      workspaceId: "workspace_current",
      workspaceFingerprint: `sha256:${"9".repeat(64)}`,
    });
    expect(
      dispatcher.bind({
        action: "create",
        source: dispatcher.captureCurrent(),
        after: current,
      }),
    ).toBe(true);
    const guardedDispatcher = {
      ...dispatcher,
      resetAll: vi.fn(dispatcher.resetAll),
      clearCurrentSource: vi.fn(dispatcher.clearCurrentSource),
      bind: vi.fn(dispatcher.bind),
    };

    const destinations = createProductionDestinationStore();
    const capturedWorkflow = {};
    const oldCommittedWorkflow = {};
    const currentWorkflow = {};
    const captured = destinations.bindProject(capturedWorkflow, production);
    destinations.bindProject(currentWorkflow, current);
    const currentBinding = Object.freeze({
      productionWorkspaceHandle: current.workspaceHandle,
      productionWorkspaceId: current.workspaceId,
      contextWorkspaceHandle: "context.current",
    });
    const session = {
      workspaceState: { status: "awaiting" },
      transactionTransparency: undefined,
      acceptedGenerationSequence: undefined,
      productionContextBinding: currentBinding,
      productionSessionHandle: current.workspaceHandle,
      productionState: { status: "ready", projection: current },
    };
    const ctx = {
      session,
      deps: {
        productionProposalDispatcher: guardedDispatcher,
        productionDestinations: destinations,
        appModeController: { activeWorkflow: () => currentWorkflow },
        pageRegistry: { register: vi.fn() },
      },
      actions: {
        currentProductionProjection: () => current,
        rememberAcceptedProjectionPromptId: vi.fn(),
        writeProductionSessionHandle: vi.fn(),
        renderCurrent: vi.fn(),
      },
    } as unknown as ShellRuntime;
    const admission = Object.freeze({
      requestId: "production.admit.old",
      destination: captured,
      target: null,
      workflow: capturedWorkflow,
    });
    const bootstrap = {
      workspace: {
        workspace_id: "context.old",
        report_revision: 1,
        report_fingerprint: `sha256:${"1".repeat(64)}`,
      },
      transactionTransparency: undefined,
      projection: { correlation: { prompt_id: "prompt.old" } },
    } as unknown as Parameters<typeof installManagedMemberProject>[1];

    expect(
      installManagedMemberProject(
        ctx,
        bootstrap,
        production,
        admission,
        oldCommittedWorkflow,
      ).viewUpdated,
    ).toBe(false);
    expect({
      actionable: dispatcher.rows(current)[0]?.actionable,
      contextBinding: session.productionContextBinding,
      resetCalls: guardedDispatcher.resetAll.mock.calls.length,
      clearCalls: guardedDispatcher.clearCurrentSource.mock.calls.length,
      bindCalls: guardedDispatcher.bind.mock.calls.length,
    }).toEqual({
      actionable: true,
      contextBinding: currentBinding,
      resetCalls: 0,
      clearCalls: 0,
      bindCalls: 0,
    });
  });

  it("keeps the current proposal owner when an old refresh finishes after workflow change", async () => {
    const dispatcher = createProductionProposalDispatcher({ send: vi.fn() });
    const guardedDispatcher = {
      ...dispatcher,
      bind: vi.fn(dispatcher.bind),
      refreshProjection: vi.fn(dispatcher.refreshProjection),
    };
    const proposalFingerprint = `sha256:${"d".repeat(64)}`;
    dispatcher.observe(
      {
        workspace_id: "context.current",
        report_revision: 1,
        report_fingerprint: proposalFingerprint,
      },
      decodeSemanticProposalReviewHandle({
        schema: "h3.context.semantic_proposal_review_handle.v1",
        review_id: `review_${"t".repeat(32)}`,
        transaction_fingerprint: `sha256:${"b".repeat(64)}`,
        workspace_fingerprint: `sha256:${"c".repeat(64)}`,
        report_fingerprint: proposalFingerprint,
        correlation: { prompt_id: "prompt.current", execution_node_id: "17" },
        available: true,
        reason: "review_available",
      }),
    );
    const current = Object.freeze({
      ...production,
      workspaceHandle: `pw_${"y".repeat(43)}`,
      workspaceId: "workspace_current_refresh",
      workspaceFingerprint: `sha256:${"8".repeat(64)}`,
    });
    expect(
      dispatcher.bind({
        action: "create",
        source: dispatcher.captureCurrent(),
        after: current,
      }),
    ).toBe(true);

    const refreshed = Object.freeze({
      ...production,
      workspaceRevision: production.workspaceRevision + 1,
      workspaceFingerprint: `sha256:${"7".repeat(64)}`,
    });
    const destinations = createProductionDestinationStore();
    const capturedWorkflow = {};
    const currentWorkflow = {};
    const captured = destinations.bindProject(capturedWorkflow, production);
    destinations.bindProject(currentWorkflow, current);
    const admission = Object.freeze({
      requestId: "production.admit.old.refresh",
      destination: captured,
      target: {
        workspaceHandle: production.workspaceHandle,
        workspaceId: production.workspaceId,
        segmentId: null,
      },
      workflow: capturedWorkflow,
    });
    const managed = {
      completed: false,
      projectMember: {
        admission,
        member,
        projection: production,
      },
    } as unknown as NonNullable<ShellRuntime["session"]["activeManagedRun"]>;
    const session = {
      activeManagedRun: managed,
      productionSessionHandle: current.workspaceHandle,
      productionState: { status: "ready", projection: current },
    };
    const ctx = {
      session,
      deps: {
        productionProposalDispatcher: guardedDispatcher,
        productionDestinations: destinations,
        productionActions: {
          send: vi.fn().mockResolvedValue({
            status: 200,
            projection: refreshed,
          }),
        },
        appModeController: { activeWorkflow: () => currentWorkflow },
      },
      actions: {
        currentProductionProjection: () => current,
        writeProductionSessionHandle: vi.fn(),
      },
    } as unknown as ShellRuntime;

    await refreshManagedMemberProject(
      ctx,
      managed,
      "production.read_member.old.refresh",
      false,
    );
    expect(guardedDispatcher.bind).not.toHaveBeenCalled();
    expect(guardedDispatcher.refreshProjection).not.toHaveBeenCalled();
    expect(dispatcher.rows(current)[0]).toMatchObject({ actionable: true });
    expect(session.productionSessionHandle).toBe(current.workspaceHandle);
  });
});
