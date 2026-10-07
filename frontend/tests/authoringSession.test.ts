import { describe, expect, it, vi } from "vitest";
import compositionFixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";

import type { AuthoringViewState } from "../src/state/authoringViewState";
import {
  decodeTimelineHistoryProjection,
  type TimelineCommandWire,
  type TimelineHistoryProjection,
  type TimelineReceipt,
} from "../src/contracts/authoringWorkbenchCodec";
import { AuthoringClientError } from "../src/host/authoringActions";
import { createAuthoringSession } from "../src/host/authoringSession";
import { createNleWorkspaceSession } from "../src/lifecycle/nleWorkspaceSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import { projectionFixture } from "./support/authoringFixture";
import { expandedState } from "./support/nleWorkspaceBinding";

const CURSOR = `h3.context.timeline_history_cursor.v1:1:${"a".repeat(64)}`;

function history(
  overrides: Record<string, unknown> = {},
): TimelineHistoryProjection {
  return decodeTimelineHistoryProjection({
    schema: "h3.context.timeline_history_projection.v1",
    workspace_handle: compositionFixture.snapshot.workspace_handle,
    snapshot: compositionFixture.snapshot,
    selection: [],
    undo_cursor: null,
    redo_cursor: null,
    rejection: null,
    ...overrides,
  });
}

function receipt(
  accepted: TimelineHistoryProjection,
  commands: readonly TimelineCommandWire[],
): TimelineReceipt {
  const snapshot = accepted.snapshot;
  return Object.freeze({
    schema: "h3.context.timeline_receipt.v1",
    requestId: "placeholder",
    transactionId: "placeholder",
    workspaceHandle: snapshot.workspaceHandle,
    beforeWorkspaceRevision: snapshot.workspaceRevision,
    afterWorkspaceRevision: snapshot.workspaceRevision + 1,
    beforeWorkspaceFingerprint: snapshot.workspaceFingerprint,
    afterWorkspaceFingerprint: snapshot.workspaceFingerprint,
    beforeTimelineRevision: snapshot.timelineRevision,
    afterTimelineRevision: snapshot.timelineRevision + 1,
    beforeTimelineFingerprint: snapshot.timelineFingerprint,
    afterTimelineFingerprint: snapshot.timelineFingerprint,
    commands,
    affectedIds: Object.freeze([]),
    inverse: Object.freeze({
      kind: "restore_transaction_state",
      historyCursor: CURSOR,
    }),
    historyCursor: CURSOR,
    selection: Object.freeze([]),
    snapshot,
  });
}

function subject(
  state: AuthoringViewState,
  send: ReturnType<typeof vi.fn>,
  renderCurrent: ReturnType<typeof vi.fn> = vi.fn(),
) {
  const session = createShellSession();
  session.authoringState = state;
  const controller = createAuthoringSession({
    session,
    deps: { authoringActions: { send } },
    actions: { renderCurrent },
  } as unknown as ShellRuntime);
  return { controller, session, renderCurrent };
}

describe("M25-11 authoring session history integration", () => {
  it("initializes an unopened history with V2 selectors and adopts the accepted snapshot", async () => {
    const accepted = history();
    const send = vi.fn(async () => ({
      status: 200 as const,
      history: accepted,
    }));
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const { controller, session } = subject(
      { status: "ready", projection },
      send,
    );
    await controller.runAuthoringIntent({
      action: "initialize_timeline_history",
    });
    expect(send).toHaveBeenCalledExactlyOnceWith(
      expect.stringMatching(/^authoring-/),
      "initialize_timeline_history",
      {
        workspace_handle: projection.workspaceHandle,
        authoring_schema: "h3.context.nle_authoring_state.v1",
        profile_id: "h3.authoring.nle_content_extent.v1",
        operation_profile_id: "h3.authoring.nle_operation.v2",
        expected_reference_revision: projection.reference.revision,
        expected_timeline_revision: projection.timeline.revision,
      },
      projection.workspaceHandle,
    );
    expect(session.authoringState).toMatchObject({
      status: "ready",
      timelineHistory: accepted,
    });
  });

  it("reads current history after initialization conflict without replaying or replacing its seed", async () => {
    const accepted = history();
    const send = vi
      .fn()
      .mockRejectedValueOnce(
        new AuthoringClientError("timeline_initialization_conflict", 409),
      )
      .mockResolvedValueOnce({ status: 200, history: accepted });
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const { controller, session } = subject(
      { status: "ready", projection },
      send,
    );
    await controller.runAuthoringIntent({
      action: "initialize_timeline_history",
    });
    expect(send.mock.calls.map((call) => call[1])).toEqual([
      "initialize_timeline_history",
      "read_timeline_history",
    ]);
    expect(session.authoringState).toMatchObject({
      status: "ready",
      timelineHistory: accepted,
    });
  });

  it("adopts an explicitly read backend history without replacing the legacy projection", async () => {
    const accepted = history();
    const send = vi.fn(async () => ({
      status: 200 as const,
      history: accepted,
    }));
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const { controller, session } = subject(
      { status: "ready", projection },
      send,
    );

    await controller.runAuthoringIntent({ action: "read_timeline_history" });

    expect(send).toHaveBeenCalledWith(
      expect.stringMatching(/^authoring-/),
      "read_timeline_history",
      { workspace_handle: projection.workspaceHandle },
      projection.workspaceHandle,
    );
    expect(session.authoringState).toMatchObject({
      status: "ready",
      projection,
      timelineHistory: accepted,
    });
  });

  it("submits exact accepted CAS and refreshes branch heads after the receipt", async () => {
    const accepted = history();
    const commands = Object.freeze([
      Object.freeze({
        kind: "select_clips" as const,
        payload: Object.freeze({ clip_ids: ["clip-main"] }),
      }),
    ]);
    const acceptedReceipt = receipt(accepted, commands);
    const refreshed = history({
      selection: ["clip-main"],
      undo_cursor: CURSOR,
    });
    type RefreshResult = Readonly<{
      status: 200;
      history: TimelineHistoryProjection;
    }>;
    let resolveRefresh: ((result: RefreshResult) => void) | undefined;
    const refreshResult = new Promise<RefreshResult>((resolve) => {
      resolveRefresh = resolve;
    });
    const send = vi
      .fn()
      .mockResolvedValueOnce({ status: 200 as const, receipt: acceptedReceipt })
      .mockReturnValueOnce(refreshResult);
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const { controller, session } = subject(
      { status: "ready", projection, timelineHistory: accepted },
      send,
    );

    const running = controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands,
    });

    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(session.authoringState).toEqual({
      status: "pending",
      projection,
      lastTimelineReceipt: acceptedReceipt,
    });
    if (resolveRefresh === undefined)
      throw new Error("refresh resolver absent");
    resolveRefresh({ status: 200, history: refreshed });
    await running;

    const [requestId, action, payload, handle] = send.mock.calls[0] ?? [];
    expect(action).toBe("apply_timeline_transaction");
    expect(handle).toBe(accepted.workspaceHandle);
    expect(payload).toMatchObject({
      request_id: requestId,
      transaction_id: `tx-${requestId}`,
      workspace_handle: accepted.workspaceHandle,
      expected_workspace_revision: accepted.snapshot.workspaceRevision,
      expected_timeline_revision: accepted.snapshot.timelineRevision,
      expected_timeline_fingerprint: accepted.snapshot.timelineFingerprint,
      commands,
    });
    expect(send.mock.calls[1]?.[1]).toBe("read_timeline_history");
    expect(session.authoringState).toMatchObject({
      status: "ready",
      projection,
      timelineHistory: refreshed,
      lastTimelineReceipt: acceptedReceipt,
    });
  });

  it("preserves a captured gesture CAS instead of replacing it with newer accepted history", async () => {
    const accepted = history();
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const send = vi.fn(async () => ({ status: 409, history: accepted }));
    const { controller } = subject(
      { status: "ready", projection, timelineHistory: accepted },
      send,
    );
    await controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands: [
        {
          kind: "trim_clip",
          payload: { clip_id: "clip-0", edge: "end", delta_frames: -1 },
        },
      ],
      capturedTimeline: {
        workspaceHandle: accepted.workspaceHandle,
        workspaceRevision: 0,
        timelineRevision: 0,
        timelineFingerprint: "sha256:" + "b".repeat(64),
      },
    });
    expect(send).toHaveBeenCalledTimes(1);
    expect((send.mock.calls as unknown[][])[0]?.[2]).toMatchObject({
      expected_workspace_revision: 0,
      expected_timeline_revision: 0,
      expected_timeline_fingerprint: "sha256:" + "b".repeat(64),
    });
  });

  it("adopts a 409 projection and drops the stale receipt instead of retrying locally", async () => {
    const accepted = history({ undo_cursor: CURSOR });
    const conflict = history({
      undo_cursor: null,
      redo_cursor: CURSOR,
      rejection: { code: "stale_workspace_revision" },
    });
    const send = vi.fn(async () => ({
      status: 409 as const,
      history: conflict,
    }));
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const { controller, session } = subject(
      {
        status: "ready",
        projection,
        timelineHistory: accepted,
        lastTimelineReceipt: receipt(accepted, [
          { kind: "select_clips", payload: { clip_ids: [] } },
        ]),
      },
      send,
    );

    await controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands: [{ kind: "undo", payload: { history_cursor: CURSOR } }],
    });

    expect(send).toHaveBeenCalledTimes(1);
    expect(session.authoringState).toEqual({
      status: "conflict",
      projection,
      timelineHistory: conflict,
    });
  });

  it("retains an accepted receipt but drops stale history when branch refresh fails", async () => {
    const accepted = history();
    const commands = Object.freeze([
      Object.freeze({
        kind: "select_clips" as const,
        payload: Object.freeze({ clip_ids: ["clip-main"] }),
      }),
    ]);
    const acceptedReceipt = receipt(accepted, commands);
    const send = vi
      .fn()
      .mockResolvedValueOnce({ status: 200 as const, receipt: acceptedReceipt })
      .mockRejectedValueOnce(new Error("refresh failed"));
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const { controller, session } = subject(
      { status: "ready", projection, timelineHistory: accepted },
      send,
    );

    await controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands,
    });

    expect(send).toHaveBeenCalledTimes(2);
    expect(session.authoringState).toEqual({
      status: "error",
      projection,
      reason: "internal_failure",
      lastTimelineReceipt: acceptedReceipt,
    });
  });

  it("does not resurrect stale history when the accepted follow-up omits history", async () => {
    const accepted = history();
    const commands = Object.freeze([
      Object.freeze({
        kind: "select_clips" as const,
        payload: Object.freeze({ clip_ids: ["clip-main"] }),
      }),
    ]);
    const acceptedReceipt = receipt(accepted, commands);
    const send = vi
      .fn()
      .mockResolvedValueOnce({ status: 200 as const, receipt: acceptedReceipt })
      .mockResolvedValueOnce({ status: 200 as const });
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const { controller, session } = subject(
      { status: "ready", projection, timelineHistory: accepted },
      send,
    );

    await controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands,
    });

    expect(session.authoringState).toEqual({
      status: "error",
      projection,
      reason: "internal_failure",
      lastTimelineReceipt: acceptedReceipt,
    });
  });

  it("drops every workspace-bound projection receipt and cursor after a terminal response", async () => {
    const accepted = history({ undo_cursor: CURSOR });
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const send = vi.fn(async () => {
      throw new AuthoringClientError("workspace_gone", 410);
    });
    const { controller, session } = subject(
      {
        status: "ready",
        projection,
        timelineHistory: accepted,
        lastTimelineReceipt: receipt(accepted, [
          { kind: "select_clips", payload: { clip_ids: [] } },
        ]),
      },
      send,
    );

    await controller.runAuthoringIntent({ action: "read_timeline_history" });

    expect(session.authoringState).toEqual({
      status: "gone",
      reason: "workspace_gone",
    });
  });

  it("drops an accepted receipt when its follow-up history read says the workspace is gone", async () => {
    const accepted = history();
    const commands = Object.freeze([
      Object.freeze({
        kind: "select_clips" as const,
        payload: Object.freeze({ clip_ids: ["clip-main"] }),
      }),
    ]);
    const acceptedReceipt = receipt(accepted, commands);
    const send = vi
      .fn()
      .mockResolvedValueOnce({ status: 200 as const, receipt: acceptedReceipt })
      .mockRejectedValueOnce(new AuthoringClientError("workspace_gone", 410));
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const { controller, session } = subject(
      { status: "ready", projection, timelineHistory: accepted },
      send,
    );

    await controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands,
    });

    expect(session.authoringState).toEqual({
      status: "gone",
      reason: "workspace_gone",
    });
  });
});

describe("M25-16 corrective F1: unknown transaction outcome", () => {
  const commands = Object.freeze([
    Object.freeze({
      kind: "select_clips" as const,
      payload: Object.freeze({ clip_ids: ["clip-main"] }),
    }),
  ]);
  const lost = () => new AuthoringClientError("transport_failure", 0);

  function unknownSubject(send: ReturnType<typeof vi.fn>) {
    const accepted = history();
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const rendered: AuthoringViewState[] = [];
    const renderCurrent = vi.fn();
    const built = subject(
      { status: "ready", projection, timelineHistory: accepted },
      send,
      renderCurrent,
    );
    renderCurrent.mockImplementation(() => {
      rendered.push(built.session.authoringState);
    });
    return { ...built, accepted, projection, rendered };
  }

  it("marks a transport-lost transaction unknown and reconciles it with one bounded history read", async () => {
    const refreshed = history({ selection: ["clip-main"] });
    const send = vi
      .fn()
      .mockRejectedValueOnce(lost())
      .mockResolvedValueOnce({ status: 200 as const, history: refreshed });
    const { controller, session, accepted, projection, rendered } =
      unknownSubject(send);

    await controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands,
    });

    // Exactly one transaction and one read-only reconciliation: nothing is re-sent.
    expect(send).toHaveBeenCalledTimes(2);
    const [requestId, action] = send.mock.calls[0] as [string, string];
    expect(action).toBe("apply_timeline_transaction");
    expect(send.mock.calls[1]?.slice(1, 3)).toEqual([
      "read_timeline_history",
      { workspace_handle: accepted.workspaceHandle },
    ]);
    // The unknown state was rendered with the submitted identity retained for attribution and
    // the pre-transaction history still visible, but with no receipt claimed.
    expect(rendered).toContainEqual({
      status: "error",
      projection,
      reason: "outcome_unknown",
      timelineHistory: accepted,
      outcomeUnknown: {
        requestId,
        transactionId: `tx-${requestId}`,
        expectedWorkspaceRevision: accepted.snapshot.workspaceRevision,
        expectedTimelineRevision: accepted.snapshot.timelineRevision,
        expectedTimelineFingerprint: accepted.snapshot.timelineFingerprint,
        // R2-F1: the read is still in flight here, so the state may not claim it completed.
        reconciliation: "pending",
      },
    });
    // The refreshed history is adopted as the current accepted state without a receipt.
    expect(session.authoringState).toEqual({
      status: "ready",
      projection,
      timelineHistory: refreshed,
    });
  });

  it("keeps the unknown outcome and replays nothing when the reconciliation read fails", async () => {
    const send = vi
      .fn()
      .mockRejectedValueOnce(lost())
      .mockRejectedValueOnce(new Error("refresh failed"));
    const { controller, session, accepted, projection } = unknownSubject(send);

    await controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands,
    });

    expect(send).toHaveBeenCalledTimes(2);
    const [requestId] = send.mock.calls[0] as [string];
    expect(session.authoringState).toEqual({
      status: "error",
      projection,
      reason: "outcome_unknown",
      timelineHistory: accepted,
      outcomeUnknown: {
        requestId,
        transactionId: `tx-${requestId}`,
        expectedWorkspaceRevision: accepted.snapshot.workspaceRevision,
        expectedTimelineRevision: accepted.snapshot.timelineRevision,
        expectedTimelineFingerprint: accepted.snapshot.timelineFingerprint,
        // R2-F1: uncertainty is kept, and the failed read is stated as a failed read.
        reconciliation: "failed",
      },
    });
  });

  it("reconciles every outcome the backend never established, not only a rejected fetch", async () => {
    // R2-F1: the request left the browser in each of these; only a decoded 409 body or a bound
    // receipt establishes an outcome, so each one takes the read-only reconciliation path.
    const codes = [
      new AuthoringClientError("transport_failure", 0),
      new AuthoringClientError("response_body_unavailable", 200),
      new AuthoringClientError("outcome_evidence_undecodable", 200),
      new AuthoringClientError("timeline_response_mismatch", 500),
    ];
    for (const failure of codes) {
      const refreshed = history({ selection: ["clip-main"] });
      const send = vi
        .fn()
        .mockRejectedValueOnce(failure)
        .mockResolvedValueOnce({ status: 200 as const, history: refreshed });
      const { controller, session, accepted, projection, rendered } =
        unknownSubject(send);

      await controller.runAuthoringIntent({
        action: "apply_timeline_commands",
        commands,
      });

      expect(send).toHaveBeenCalledTimes(2);
      expect(send.mock.calls[0]?.[1]).toBe("apply_timeline_transaction");
      expect(send.mock.calls[1]?.slice(1, 3)).toEqual([
        "read_timeline_history",
        { workspace_handle: accepted.workspaceHandle },
      ]);
      const unknown = rendered.find(
        (state) =>
          state.status === "error" && state.reason === "outcome_unknown",
      );
      expect(unknown, failure.code).toBeDefined();
      expect(unknown).toMatchObject({
        timelineHistory: accepted,
        outcomeUnknown: { reconciliation: "pending" },
      });
      // No receipt is claimed for a mutation whose outcome was never established.
      expect(unknown).not.toHaveProperty("lastTimelineReceipt");
      expect(session.authoringState).toEqual({
        status: "ready",
        projection,
        timelineHistory: refreshed,
      });
    }
  });

  it("drops to gone when the reconciliation read says the workspace is terminal", async () => {
    const send = vi
      .fn()
      .mockRejectedValueOnce(lost())
      .mockRejectedValueOnce(new AuthoringClientError("workspace_gone", 410));
    const { controller, session } = unknownSubject(send);

    await controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands,
    });

    expect(send).toHaveBeenCalledTimes(2);
    expect(session.authoringState).toEqual({
      status: "gone",
      reason: "workspace_gone",
    });
  });

  it("keeps a decoded transaction failure on its own code without a reconciliation read", async () => {
    const send = vi
      .fn()
      .mockRejectedValueOnce(
        new AuthoringClientError("unexpected_status", 500),
      );
    const { controller, session, accepted, projection } = unknownSubject(send);

    await controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands,
    });

    expect(send).toHaveBeenCalledTimes(1);
    expect(session.authoringState).toEqual({
      status: "error",
      projection,
      reason: "unexpected_status",
      timelineHistory: accepted,
    });
  });

  it("does not adopt a reconciliation read that lands after the unknown state was replaced", async () => {
    let resolveRead!: (value: unknown) => void;
    const send = vi
      .fn()
      .mockRejectedValueOnce(lost())
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveRead = resolve;
          }),
      );
    const { controller, session } = unknownSubject(send);

    const run = controller.runAuthoringIntent({
      action: "apply_timeline_commands",
      commands,
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(session.authoringState.status).toBe("error");
    session.authoringState = { status: "released" };
    resolveRead({
      status: 200,
      history: history({ selection: ["clip-main"] }),
    });
    await run;

    expect(session.authoringState).toEqual({ status: "released" });
  });
});

describe("M25-16 late timeline replies against a destroyed view", () => {
  it("adopts a history refresh that lands after view destroy without reopening the overlay", async () => {
    // Section 6 row 4: a late reply on the timeline path may update the sidebar's authoring
    // state, but it never revives the expanded view the user already destroyed.
    const accepted = history();
    const commands = Object.freeze([
      Object.freeze({
        kind: "select_clips" as const,
        payload: Object.freeze({ clip_ids: ["clip-main"] }),
      }),
    ]);
    const acceptedReceipt = receipt(accepted, commands);
    const refreshed = history({
      selection: ["clip-main"],
      undo_cursor: CURSOR,
    });
    let resolveRefresh:
      | ((result: { status: 200; history: TimelineHistoryProjection }) => void)
      | undefined;
    const send = vi
      .fn()
      .mockResolvedValueOnce({ status: 200 as const, receipt: acceptedReceipt })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveRefresh = resolve;
        }),
      );
    const projection = projectionFixture({
      workspace_handle: accepted.workspaceHandle,
    });
    const session = createShellSession();
    session.authoringState = {
      status: "ready",
      projection,
      timelineHistory: accepted,
    };
    session.nleWorkspace = expandedState();
    const renderCurrent = vi.fn();
    const runtime = {
      session,
      deps: {
        authoringActions: { send },
        authoringOutputCapabilityClient: { read: vi.fn() },
      },
      actions: {
        renderCurrent,
        selectPage: vi.fn(),
        runAuthoringIntent: (intent: unknown) =>
          authoring.runAuthoringIntent(intent as never),
      },
    } as unknown as ShellRuntime;
    const authoring = createAuthoringSession(runtime);
    const nle = createNleWorkspaceSession(runtime);

    const running = nle.nleDispatchTimeline({
      action: "apply_timeline_commands",
      commands,
    });
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(session.authoringState).toMatchObject({
      status: "pending",
      lastTimelineReceipt: acceptedReceipt,
    });
    nle.nleDisposeOverlay();
    expect(session.nleWorkspace.surface).toMatchObject({
      status: "compact_ready",
      lastCloseReason: "view_destroy",
    });
    const generation = session.nleWorkspace.surface.generation;
    const rendersBeforeReply = renderCurrent.mock.calls.length;

    if (resolveRefresh === undefined)
      throw new Error("refresh resolver absent");
    resolveRefresh({ status: 200, history: refreshed });
    await running;

    expect(session.authoringState).toMatchObject({
      status: "ready",
      timelineHistory: refreshed,
      lastTimelineReceipt: acceptedReceipt,
    });
    expect(session.nleWorkspace.surface).toMatchObject({
      status: "compact_ready",
      generation,
      lastCloseReason: "view_destroy",
    });
    // The compact sidebar re-renders the accepted state; nothing requests the overlay again.
    expect(renderCurrent.mock.calls.length).toBeGreaterThan(rendersBeforeReply);
    expect(send).toHaveBeenCalledTimes(2);
  });
});
