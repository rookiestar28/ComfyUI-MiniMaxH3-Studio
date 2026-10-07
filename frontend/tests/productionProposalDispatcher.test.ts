import { describe, expect, it, vi } from "vitest";

import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import {
  decodeSemanticProposalActionResult,
  decodeSemanticProposalReviewHandle,
} from "../src/contracts/semanticProposalReviewCodec";
import { createProductionProposalDispatcher } from "../src/host/productionProposalDispatcher";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";

const fp = (value: string) => `sha256:${value.repeat(64)}`;
const handle = decodeSemanticProposalReviewHandle({
  schema: "h3.context.semantic_proposal_review_handle.v1",
  review_id: `review_${"a".repeat(32)}`,
  transaction_fingerprint: fp("b"),
  workspace_fingerprint: fp("c"),
  report_fingerprint: fp("d"),
  correlation: { prompt_id: "prompt.1", execution_node_id: "node.1" },
  available: true,
  reason: "review_available",
});
const actionIdentity = Object.freeze({
  workspace_id: "context.workspace",
  report_revision: 1,
  report_fingerprint: fp("d"),
});

function production(
  segmentIds: readonly string[],
  workspaceFingerprint = fp("f"),
  workspaceRevision = segmentIds.length,
  workspaceHandle = `pw_${"e".repeat(43)}`,
) {
  return decodeProductionWorkbenchProjection({
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: workspaceHandle,
    workspace_id: "production.workspace",
    workspace_revision: workspaceRevision,
    workspace_fingerprint: workspaceFingerprint,
    segments: segmentIds.map((segmentId, index) => ({
      segment_id: segmentId,
      ordinal: index + 1,
      task_mode: "t2va",
      duration: {
        duration_milliseconds: 5167,
        delivered_milliseconds: 5167,
        frame_count: 124,
        snapped: false,
      },
      relation: "independent",
      predecessor_segment_id: null,
      boundary_kind: "independent",
      closure_state: "clean",
      job_state: "unavailable",
      artifact_state: "unavailable",
      continuity_state: "unavailable",
      delivered_geometry: null,
    })),
    selected_segment_ids: [...segmentIds],
    run: { state: "unavailable", completed: 0, total: segmentIds.length },
    generation_sequence: null,
    reconstruction: { state: "unavailable" },
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: [],
    outputs: [],
    allowed_actions: ["set_selection", "read_projection", "release_workspace"],
    blocker_codes: [],
    limits: { max_segments: 64, max_outputs: 65 },
  });
}

function reviewResult(
  summary = "Bounded proposal summary",
  segmentId = "source.segment",
) {
  return decodeSemanticProposalActionResult({
    schema: "h3.context.semantic_proposal.action_result.v1",
    outcome: "read",
    reason: "review_read",
    review: {
      schema: "h3.context.semantic_proposal_review.v1",
      review_id: handle.review_id,
      transaction_fingerprint: handle.transaction_fingerprint,
      workspace_id: "proposal.workspace",
      workspace_revision: 1,
      workspace_fingerprint: handle.workspace_fingerprint,
      report_fingerprint: handle.report_fingerprint,
      attempt: 1,
      revision: 1,
      state: "ready_for_review",
      segment_id: segmentId,
      correlation: handle.correlation,
      changed_collections: ["scenes"],
      groups: [
        {
          collection: "scenes",
          items: [
            {
              target_id: "scene.1",
              summary,
              change_kind: "modified",
              reference_labels: [],
              constraint_labels: [],
              uncertainty_codes: [],
              reason_code: "semantic_delta",
            },
          ],
        },
      ],
      clarifications: [],
      uncertainty_codes: [],
      reason_code: "review_ready",
      actions: {
        proposal_read: true,
        proposal_resolve: false,
        proposal_accept: true,
        proposal_reject: true,
        proposal_cancel: true,
        edit: false,
        regenerate: false,
      },
      action_reasons: {
        edit: "source_owner_unavailable",
        regenerate: "source_owner_unavailable",
      },
      terminal: null,
    },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

describe("Production proposal dispatcher", () => {
  it("admits only a narrow identity and binds exact successful lineage", () => {
    const dispatcher = createProductionProposalDispatcher({
      send: vi.fn(),
    });
    expect(dispatcher.observe(actionIdentity, handle)).toBe("admitted");
    expect(dispatcher.observe(actionIdentity, handle)).toBe("idempotent");

    const source = dispatcher.captureCurrent();
    const one = production(["segment.1"]);
    expect(dispatcher.bind({ action: "create", source, after: one })).toBe(
      true,
    );
    expect(dispatcher.rows(one)).toMatchObject([
      { segmentId: "segment.1", status: "not_issued" },
    ]);

    const adopted = production(["segment.1", "segment.2"]);
    dispatcher.resetWorkspace();
    expect(dispatcher.bind({ action: "create", source, after: adopted })).toBe(
      false,
    );
    expect(dispatcher.rows(adopted).map((row) => row.status)).toEqual([
      "unavailable",
      "unavailable",
    ]);
    expect(JSON.stringify(dispatcher)).not.toContain("prompt_text");
  });

  it("advances only a verified local Production transition and exact replace", () => {
    const dispatcher = createProductionProposalDispatcher({ send: vi.fn() });
    dispatcher.observe(actionIdentity, handle);
    const source = dispatcher.captureCurrent();
    const before = production(["segment.1"]);
    expect(dispatcher.bind({ action: "create", source, after: before })).toBe(
      true,
    );
    const advanced = production(["segment.1"], fp("8"), 2);
    expect(dispatcher.advanceProjection(before, advanced)).toBe(true);
    expect(dispatcher.rows(advanced)).toMatchObject([{ status: "not_issued" }]);

    const replaced = production(["segment.1"], fp("9"), 3);
    expect(
      dispatcher.bind({
        action: "replace",
        source,
        before: advanced,
        after: replaced,
        segmentId: "segment.1",
      }),
    ).toBe(true);
    expect(dispatcher.rows(replaced)).toMatchObject([{ status: "not_issued" }]);

    const drifted = production(["segment.1"], fp("0"), 4);
    expect(dispatcher.advanceProjection(before, drifted)).toBe(false);
    expect(dispatcher.captureCurrent()).toBeUndefined();
  });

  it("fails closed when an unobserved replacement may precede an append", () => {
    const dispatcher = createProductionProposalDispatcher({ send: vi.fn() });
    dispatcher.observe(actionIdentity, handle);
    const before = production(["segment.1"], fp("7"), 1);
    expect(
      dispatcher.bind({
        action: "create",
        source: dispatcher.captureCurrent(),
        after: before,
      }),
    ).toBe(true);

    // Revision 2 was not observed and may have rebound segment.1 to another Context.
    const after = production(["segment.1", "segment.2"], fp("9"), 3);
    expect(dispatcher.bind({ action: "add", before, after })).toBe(false);
    expect(dispatcher.rows(after)[0]).toMatchObject({
      segmentId: "segment.1",
      status: "stale",
      actionable: false,
    });
  });

  it("keeps exact existing bindings when an appended segment has no proposal source", () => {
    const dispatcher = createProductionProposalDispatcher({ send: vi.fn() });
    dispatcher.observe(actionIdentity, handle);
    const source = dispatcher.captureCurrent();
    const before = production(["segment.1"]);
    expect(dispatcher.bind({ action: "create", source, after: before })).toBe(
      true,
    );

    dispatcher.clearCurrentSource();
    const after = production(["segment.1", "segment.2"], fp("8"), 2);
    expect(dispatcher.bind({ action: "add", before, after })).toBe(true);
    expect(dispatcher.rows(after)).toMatchObject([
      {
        segmentId: "segment.1",
        status: "not_issued",
        actionable: true,
      },
      {
        segmentId: "segment.2",
        status: "unavailable",
        actionable: false,
        reason: "not_provided",
      },
    ]);
  });

  it("invalidates only the replaced segment when its replacement has no proposal source", () => {
    const dispatcher = createProductionProposalDispatcher({ send: vi.fn() });
    dispatcher.observe(actionIdentity, handle);
    const source = dispatcher.captureCurrent();
    const one = production(["segment.1"]);
    const appended = production(["segment.1", "segment.2"], fp("8"), 2);
    expect(dispatcher.bind({ action: "create", source, after: one })).toBe(
      true,
    );
    expect(
      dispatcher.bind({ action: "add", source, before: one, after: appended }),
    ).toBe(true);

    dispatcher.clearCurrentSource();
    const after = production(["segment.1", "segment.2"], fp("9"), 3);
    expect(
      dispatcher.bind({
        action: "replace",
        before: appended,
        after,
        segmentId: "segment.1",
      }),
    ).toBe(true);
    expect(dispatcher.rows(after)).toMatchObject([
      {
        segmentId: "segment.1",
        status: "unavailable",
        actionable: false,
        reason: "not_provided",
      },
      {
        segmentId: "segment.2",
        status: "not_issued",
        actionable: true,
      },
    ]);
  });

  it("fans out an immutable canonical selection with concurrency exactly two", async () => {
    const pending = [
      deferred<ReturnType<typeof reviewResult>>(),
      deferred<ReturnType<typeof reviewResult>>(),
      deferred<ReturnType<typeof reviewResult>>(),
    ];
    const send = vi.fn(() => pending[send.mock.calls.length - 1]!.promise);
    const dispatcher = createProductionProposalDispatcher({ send });
    dispatcher.observe(actionIdentity, handle);
    const source = dispatcher.captureCurrent();
    const one = production(["segment.1"]);
    const two = production(["segment.1", "segment.2"]);
    const three = production(["segment.1", "segment.2", "segment.3"]);
    dispatcher.bind({ action: "create", source, after: one });
    dispatcher.bind({ action: "add", source, before: one, after: two });
    dispatcher.bind({ action: "add", source, before: two, after: three });

    const run = dispatcher.readSelected(three, [
      "segment.3",
      "segment.1",
      "segment.2",
    ]);
    await Promise.resolve();
    expect(send).toHaveBeenCalledTimes(2);
    expect(dispatcher.rows(three).map((row) => row.status)).toEqual([
      "loading",
      "loading",
      "not_issued",
    ]);

    pending[0]!.resolve(reviewResult("first"));
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(3));
    pending[1]!.resolve(reviewResult("second"));
    pending[2]!.resolve(reviewResult("third"));
    await run;
    expect(dispatcher.rows(three).map((row) => row.status)).toEqual([
      "ready",
      "ready",
      "ready",
    ]);
  });

  it("expires sensitive rows, disposes timers and quarantines late settlement", async () => {
    let now = 0;
    const timers = new Map<number, () => void>();
    let timerId = 0;
    const late = deferred<ReturnType<typeof reviewResult>>();
    const dispatcher = createProductionProposalDispatcher({
      send: vi.fn(() => late.promise),
      now: () => now,
      schedule: (callback) => {
        timerId += 1;
        timers.set(timerId, callback);
        return timerId;
      },
      cancelSchedule: (id) => timers.delete(id as number),
    });
    dispatcher.observe(actionIdentity, handle);
    const workspace = production(["segment.1"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: workspace,
    });
    const run = dispatcher.readSelected(workspace, ["segment.1"]);
    await Promise.resolve();
    now = 900_000;
    for (const callback of [...timers.values()]) callback();
    expect(dispatcher.rows(workspace)).toMatchObject([
      {
        status: "unavailable",
        reason: "expired",
        actionable: false,
      },
    ]);
    expect(dispatcher.rows(workspace)[0]).not.toHaveProperty("reviewState");
    expect(dispatcher.contextState()).toEqual({ status: "unavailable" });

    late.resolve(reviewResult("must not return after expiry"));
    await run;
    expect(JSON.stringify(dispatcher.rows(workspace))).not.toContain(
      "must not return after expiry",
    );
    dispatcher.dispose();
    expect(timers.size).toBe(0);
  });

  it("rejects settlement after the absolute source deadline before its timer runs", async () => {
    let now = 0;
    const late = deferred<ReturnType<typeof reviewResult>>();
    const dispatcher = createProductionProposalDispatcher({
      send: vi.fn(() => late.promise),
      now: () => now,
      schedule: () => Symbol("timer"),
      cancelSchedule: () => undefined,
    });
    dispatcher.observe(actionIdentity, handle);
    const workspace = production(["segment.1"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: workspace,
    });

    now = 899_999;
    const run = dispatcher.readSelected(workspace, ["segment.1"]);
    await Promise.resolve();
    now = 900_000;
    let inspectedLateReview = 0;
    const lateResult = new Proxy(
      reviewResult("must not cross the absolute source deadline"),
      {
        get(target, key, receiver) {
          if (key === "review") inspectedLateReview += 1;
          return Reflect.get(target, key, receiver);
        },
      },
    );
    late.resolve(lateResult);
    await run;

    expect(inspectedLateReview).toBe(0);
    expect(dispatcher.rows(workspace)).toMatchObject([
      { status: "unavailable", reason: "expired" },
    ]);
    expect(JSON.stringify(dispatcher.rows(workspace))).not.toContain(
      "must not cross the absolute source deadline",
    );
  });

  it("delegates row mutation to the original source and clears sensitive state", async () => {
    const accepted = decodeSemanticProposalActionResult({
      ...reviewResult(),
      outcome: "accepted",
      reason: "proposal_accepted",
      review: {
        ...reviewResult().review,
        state: "accepted",
        terminal: "accepted",
        actions: {
          ...reviewResult().review.actions,
          proposal_accept: false,
          proposal_reject: false,
          proposal_cancel: false,
        },
      },
    });
    const send = vi
      .fn()
      .mockResolvedValueOnce(reviewResult())
      .mockResolvedValueOnce(accepted);
    const dispatcher = createProductionProposalDispatcher({ send });
    dispatcher.observe(actionIdentity, handle);
    const workspace = production(["segment.1"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: workspace,
    });
    await dispatcher.readSelected(workspace, ["segment.1"]);
    await dispatcher.action(workspace, "segment.1", {
      action: "proposal_accept",
    });
    expect(send.mock.calls[1]?.[0]).toEqual(actionIdentity);
    expect(send.mock.calls[1]?.[1]).toEqual(handle);
    expect(send.mock.calls[1]?.[2]).toMatchObject({ revision: 1 });
    expect(dispatcher.rows(workspace)).toMatchObject([
      {
        status: "terminal",
        reviewState: {
          status: "ready",
          projection: { terminal: "accepted" },
        },
      },
    ]);
    dispatcher.closeRow("segment.1");
    expect(JSON.stringify(dispatcher.rows(workspace))).not.toContain(
      "Bounded proposal summary",
    );
  });

  it("retains a resolved successor handle across close and explicit reread", async () => {
    const readable = decodeSemanticProposalActionResult({
      ...reviewResult(),
      review: {
        ...reviewResult().review,
        actions: {
          ...reviewResult().review.actions,
          proposal_resolve: true,
        },
      },
    });
    const successor = decodeSemanticProposalActionResult({
      ...reviewResult(),
      outcome: "resolved",
      reason: "proposal_resolved",
      review: {
        ...reviewResult().review,
        transaction_fingerprint: fp("e"),
        workspace_revision: 2,
        workspace_fingerprint: fp("7"),
        revision: 2,
      },
    });
    const send = vi
      .fn()
      .mockResolvedValueOnce(readable)
      .mockResolvedValueOnce(successor)
      .mockResolvedValueOnce(successor);
    const dispatcher = createProductionProposalDispatcher({ send });
    dispatcher.observe(actionIdentity, handle);
    const workspace = production(["segment.1"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: workspace,
    });

    await dispatcher.readSelected(workspace, ["segment.1"]);
    await dispatcher.action(workspace, "segment.1", {
      action: "proposal_resolve",
      resolutions: [],
    });
    dispatcher.closeRow("segment.1");
    await dispatcher.readSelected(workspace, ["segment.1"]);

    expect(send.mock.calls[2]?.[1]).toMatchObject({
      transaction_fingerprint: fp("e"),
      workspace_fingerprint: fp("7"),
    });
  });

  it("rejects invalid selected snapshots without issuing or retargeting", async () => {
    const send = vi.fn();
    const dispatcher = createProductionProposalDispatcher({ send });
    dispatcher.observe(actionIdentity, handle);
    const workspace = production(["segment.1"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: workspace,
    });
    await dispatcher.readSelected(workspace, ["segment.1", "segment.1"]);
    await dispatcher.readSelected(workspace, ["segment.foreign"]);
    expect(send).not.toHaveBeenCalled();
    expect(dispatcher.rows(workspace)).toMatchObject([
      { segmentId: "segment.1", status: "not_issued" },
    ]);
  });

  it("fails closed at source capacity and rejects conflicting review identity", () => {
    const dispatcher = createProductionProposalDispatcher({ send: vi.fn() });
    for (let index = 0; index < 64; index += 1) {
      const identity = decodeSemanticProposalReviewHandle({
        ...handle,
        review_id: `review_${index.toString(16).padStart(32, "0")}`,
      });
      expect(dispatcher.observe(actionIdentity, identity)).toBe("admitted");
    }
    const overflow = decodeSemanticProposalReviewHandle({
      ...handle,
      review_id: `review_${"f".repeat(32)}`,
    });
    expect(dispatcher.observe(actionIdentity, overflow)).toBe("capacity");
    expect(dispatcher.capacityStatus()).toBe(true);
    expect(dispatcher.captureCurrent()).toBeUndefined();
    expect(dispatcher.contextState()).toEqual({ status: "unavailable" });
    expect(dispatcher.rows(production(["unbound.segment"]))).toMatchObject([
      { status: "unavailable", reason: "capacity" },
    ]);

    const first = decodeSemanticProposalReviewHandle({
      ...handle,
      review_id: `review_${"0".repeat(32)}`,
      transaction_fingerprint: fp("e"),
    });
    expect(dispatcher.observe(actionIdentity, first)).toBe("conflict");
    expect(dispatcher.captureCurrent()).toBeUndefined();
  });

  it("lazy-joins only an exact returned workspace fingerprint and segment", async () => {
    const dispatcher = createProductionProposalDispatcher({
      send: vi.fn().mockResolvedValue(reviewResult()),
    });
    dispatcher.observe(actionIdentity, handle);
    const exact = production(["source.segment"], handle.workspace_fingerprint);
    await dispatcher.runContext({ action: "proposal_read" }, exact);
    expect(dispatcher.rows(exact)).toMatchObject([
      { segmentId: "source.segment", status: "not_issued" },
    ]);

    const foreign = production(["source.segment"], fp("9"));
    dispatcher.resetWorkspace();
    await dispatcher.runContext({ action: "proposal_read" }, foreign);
    expect(dispatcher.rows(foreign)).toMatchObject([
      { status: "unavailable", reason: "unbound" },
    ]);
  });

  it("times out at 30 seconds and explicitly stops unissued selected rows", async () => {
    let now = 0;
    const timers = new Map<number, () => void>();
    let timerId = 0;
    const pending = deferred<ReturnType<typeof reviewResult>>();
    const send = vi.fn((_action, _handle, _current, _request, signal) => {
      expect(signal?.aborted).toBe(false);
      return pending.promise;
    });
    const dispatcher = createProductionProposalDispatcher({
      send,
      now: () => now,
      schedule: (callback) => {
        timerId += 1;
        timers.set(timerId, callback);
        return timerId;
      },
      cancelSchedule: (id) => timers.delete(id as number),
    });
    dispatcher.observe(actionIdentity, handle);
    const one = production(["segment.1"]);
    const two = production(["segment.1", "segment.2"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: one,
    });
    dispatcher.bind({
      action: "add",
      source: dispatcher.captureCurrent(),
      before: one,
      after: two,
    });
    const run = dispatcher.readSelected(two, ["segment.1"]);
    await Promise.resolve();
    now = 30_000;
    for (const callback of [...timers.values()]) callback();
    await run;
    expect(dispatcher.rows(two)[0]).toMatchObject({
      status: "client_aborted",
      reason: "timeout",
    });
    dispatcher.cancelSelected(two, ["segment.2"]);
    expect(dispatcher.rows(two)[1]).toMatchObject({
      status: "client_aborted",
    });
    dispatcher.dispose();
  });

  it("stops the whole selected batch before a timed-out worker can issue another row", async () => {
    let now = 0;
    const timers = new Map<
      number,
      Readonly<{ callback: () => void; delay: number }>
    >();
    let timerId = 0;
    const admitted = [
      deferred<ReturnType<typeof reviewResult>>(),
      deferred<ReturnType<typeof reviewResult>>(),
    ];
    const send = vi.fn(() => {
      const slot = admitted[send.mock.calls.length - 1];
      return (
        slot?.promise ?? Promise.resolve(reviewResult("unexpected queued read"))
      );
    });
    const dispatcher = createProductionProposalDispatcher({
      send,
      now: () => now,
      schedule: (callback, delay) => {
        timerId += 1;
        timers.set(timerId, { callback, delay });
        return timerId;
      },
      cancelSchedule: (id) => timers.delete(id as number),
    });
    dispatcher.observe(actionIdentity, handle);
    const source = dispatcher.captureCurrent();
    const one = production(["segment.1"]);
    const two = production(["segment.1", "segment.2"]);
    const three = production(["segment.1", "segment.2", "segment.3"]);
    dispatcher.bind({ action: "create", source, after: one });
    dispatcher.bind({ action: "add", source, before: one, after: two });
    dispatcher.bind({ action: "add", source, before: two, after: three });

    const run = dispatcher.readSelected(three, [
      "segment.1",
      "segment.2",
      "segment.3",
    ]);
    await Promise.resolve();
    expect(send).toHaveBeenCalledTimes(2);
    now = 30_000;
    const requestTimeout = [...timers.values()].find(
      (timer) => timer.delay === 30_000,
    );
    expect(requestTimeout).toBeDefined();
    requestTimeout!.callback();
    await Promise.resolve();
    await Promise.resolve();
    expect(send).toHaveBeenCalledTimes(2);

    admitted[1]!.resolve(
      reviewResult("second worker settles after cancellation"),
    );
    await run;
    expect(dispatcher.rows(three)[2]).toMatchObject({
      status: "client_aborted",
    });
  });

  it("preserves a completed row when another worker times out", async () => {
    let now = 0;
    const timers = new Map<
      number,
      Readonly<{ callback: () => void; delay: number }>
    >();
    let timerId = 0;
    const admitted = [
      deferred<ReturnType<typeof reviewResult>>(),
      deferred<ReturnType<typeof reviewResult>>(),
      deferred<ReturnType<typeof reviewResult>>(),
    ];
    const send = vi.fn(() => admitted[send.mock.calls.length - 1]!.promise);
    const dispatcher = createProductionProposalDispatcher({
      send,
      now: () => now,
      schedule: (callback, delay) => {
        timerId += 1;
        timers.set(timerId, { callback, delay });
        return timerId;
      },
      cancelSchedule: (id) => timers.delete(id as number),
    });
    dispatcher.observe(actionIdentity, handle);
    const source = dispatcher.captureCurrent();
    const one = production(["segment.1"]);
    const two = production(["segment.1", "segment.2"]);
    const three = production(["segment.1", "segment.2", "segment.3"]);
    dispatcher.bind({ action: "create", source, after: one });
    dispatcher.bind({ action: "add", source, before: one, after: two });
    dispatcher.bind({ action: "add", source, before: two, after: three });

    const run = dispatcher.readSelected(three, [
      "segment.1",
      "segment.2",
      "segment.3",
    ]);
    await Promise.resolve();
    admitted[0]!.resolve(reviewResult("completed before peer timeout"));
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(3));
    now = 30_000;
    const peerTimeout = [...timers.values()].find(
      (timer) => timer.delay === 30_000,
    );
    expect(peerTimeout).toBeDefined();
    peerTimeout!.callback();
    await vi.waitFor(() =>
      expect(dispatcher.rows(three)[1]).toMatchObject({
        status: "client_aborted",
        reason: "timeout",
      }),
    );
    expect(dispatcher.rows(three)).toMatchObject([
      { status: "ready" },
      { status: "client_aborted", reason: "timeout" },
      { status: "client_aborted" },
    ]);

    admitted[2]!.resolve(reviewResult("late third result"));
    await run;
    expect(JSON.stringify(dispatcher.rows(three))).toContain(
      "completed before peer timeout",
    );
    expect(JSON.stringify(dispatcher.rows(three))).not.toContain(
      "late third result",
    );
  });

  it("evicts identity-free tombstones before retained row state exceeds 64", async () => {
    let now = 0;
    let requestedSegment = "segment.0";
    const dispatcher = createProductionProposalDispatcher({
      send: vi.fn(() =>
        Promise.resolve(reviewResult("bounded tombstone", requestedSegment)),
      ),
      now: () => now,
      schedule: () => Symbol("timer"),
      cancelSchedule: () => undefined,
    });
    let firstWorkspace: ReturnType<typeof production> | undefined;

    for (let index = 0; index < 65; index += 1) {
      requestedSegment = `segment.${index}`;
      expect(dispatcher.observe(actionIdentity, handle)).toBe("admitted");
      const workspace = production(
        [requestedSegment],
        handle.workspace_fingerprint,
      );
      firstWorkspace ??= workspace;
      await dispatcher.runContext({ action: "proposal_read" }, workspace);
      expect(dispatcher.rows(workspace)[0]).toMatchObject({
        status: "not_issued",
        actionable: true,
      });
      now += 900_000;
      dispatcher.rows(workspace);
    }

    expect(dispatcher.rows(firstWorkspace!)[0]).toMatchObject({
      status: "unavailable",
      reason: "source_unavailable",
      actionable: false,
    });
    dispatcher.dispose();
  });

  it("quarantines a response after the Production identity drifts", async () => {
    const pending = deferred<ReturnType<typeof reviewResult>>();
    const send = vi.fn(() => pending.promise);
    const dispatcher = createProductionProposalDispatcher({
      send,
    });
    dispatcher.observe(actionIdentity, handle);
    const before = production(["segment.1"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: before,
    });
    const run = dispatcher.readSelected(before, ["segment.1"]);
    await Promise.resolve();
    const after = production(["segment.1"], fp("8"), 2);
    dispatcher.refreshProjection(after);
    pending.resolve(reviewResult("must be quarantined after identity drift"));
    await run;
    expect(dispatcher.rows(after)).toMatchObject([
      { status: "stale", reason: "stale" },
    ]);
    expect(JSON.stringify(dispatcher.rows(after))).not.toContain(
      "must be quarantined after identity drift",
    );
    await dispatcher.readSelected(after, ["segment.1"]);
    expect(dispatcher.rows(after)).toMatchObject([
      { status: "stale", reason: "stale" },
    ]);
    expect(dispatcher.captureCurrent()).toBeUndefined();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("cancels the dispatcher-owned batch before queued rows can issue", async () => {
    const admitted = [
      deferred<ReturnType<typeof reviewResult>>(),
      deferred<ReturnType<typeof reviewResult>>(),
    ];
    const send = vi.fn(() => {
      const slot = admitted[send.mock.calls.length - 1];
      return (
        slot?.promise ?? Promise.resolve(reviewResult("unexpected queued read"))
      );
    });
    const dispatcher = createProductionProposalDispatcher({ send });
    dispatcher.observe(actionIdentity, handle);
    const ids = ["segment.1", "segment.2", "segment.3", "segment.4"];
    let before = production([ids[0]!]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: before,
    });
    for (let index = 1; index < ids.length; index += 1) {
      const after = production(ids.slice(0, index + 1));
      dispatcher.bind({
        action: "add",
        source: dispatcher.captureCurrent(),
        before,
        after,
      });
      before = after;
    }

    const run = dispatcher.readSelected(before, ids);
    await Promise.resolve();
    expect(send).toHaveBeenCalledTimes(2);
    dispatcher.clearSensitive();
    admitted[0]!.resolve(reviewResult("late first"));
    admitted[1]!.resolve(reviewResult("late second"));
    await run;

    expect(send).toHaveBeenCalledTimes(2);
    expect(dispatcher.rows(before).map((row) => row.status)).toEqual([
      "not_issued",
      "not_issued",
      "not_issued",
      "not_issued",
    ]);
  });

  it("keeps one selected-read batch in flight across overlapping calls", async () => {
    const pending = [
      deferred<ReturnType<typeof reviewResult>>(),
      deferred<ReturnType<typeof reviewResult>>(),
    ];
    const send = vi.fn(
      () =>
        pending[send.mock.calls.length - 1]?.promise ??
        Promise.resolve(reviewResult("overlapping read")),
    );
    const dispatcher = createProductionProposalDispatcher({ send });
    dispatcher.observe(actionIdentity, handle);
    const one = production(["segment.1"]);
    const two = production(["segment.1", "segment.2"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: one,
    });
    dispatcher.bind({
      action: "add",
      source: dispatcher.captureCurrent(),
      before: one,
      after: two,
    });

    const first = dispatcher.readSelected(two, ["segment.1", "segment.2"]);
    await Promise.resolve();
    await dispatcher.readSelected(two, ["segment.1", "segment.2"]);
    expect(send).toHaveBeenCalledTimes(2);
    pending[0]!.resolve(reviewResult("first"));
    pending[1]!.resolve(reviewResult("second"));
    await first;
  });

  it("replaces a ready row with the result of an explicit reread", async () => {
    const send = vi
      .fn()
      .mockResolvedValueOnce(reviewResult("first review"))
      .mockResolvedValueOnce(reviewResult("refreshed review"));
    const dispatcher = createProductionProposalDispatcher({ send });
    dispatcher.observe(actionIdentity, handle);
    const workspace = production(["segment.1"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: workspace,
    });

    await dispatcher.readSelected(workspace, ["segment.1"]);
    await dispatcher.readSelected(workspace, ["segment.1"]);

    expect(send).toHaveBeenCalledTimes(2);
    expect(JSON.stringify(dispatcher.rows(workspace))).toContain(
      "refreshed review",
    );
    expect(JSON.stringify(dispatcher.rows(workspace))).not.toContain(
      "first review",
    );
  });

  it("clears all source identity when the Production workspace is replaced", () => {
    const dispatcher = createProductionProposalDispatcher({ send: vi.fn() });
    dispatcher.observe(actionIdentity, handle);
    const before = production(["segment.1"]);
    dispatcher.bind({
      action: "create",
      source: dispatcher.captureCurrent(),
      after: before,
    });
    const replacement = production(
      ["segment.2"],
      fp("7"),
      1,
      `pw_${"g".repeat(43)}`,
    );
    dispatcher.refreshProjection(replacement);
    expect(dispatcher.captureCurrent()).toBeUndefined();
    expect(dispatcher.contextState()).toEqual({ status: "unavailable" });
    expect(dispatcher.rows(replacement)).toMatchObject([
      {
        status: "unavailable",
        reason: "source_unavailable",
        actionable: false,
      },
    ]);
    expect(dispatcher.rows(replacement)[0]).not.toHaveProperty("reviewState");
  });
});
