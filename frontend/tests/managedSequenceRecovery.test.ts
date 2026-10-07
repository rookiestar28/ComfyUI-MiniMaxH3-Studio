import { expect, it, vi } from "vitest";

import {
  createManagedSerialSequenceRunner,
  type ManagedSequenceMutationResult,
} from "../src/host/managedSequence";
import type {
  ManagedAppModePreflight,
  AppModeStartResult,
} from "../src/host/appMode";
import type { SequenceCoordinatorResult } from "../src/host/sequenceCoordinator";

const fp = (digit: string) => `sha256:${digit.repeat(64)}`;

it("uses a fresh prepare identity for explicit retry and replays each delivery without a second effect", async () => {
  let revision = 0;
  let attempt = 1;
  let state: ManagedSequenceMutationResult["state"] = "authorized";
  const ledger = new Map<
    string,
    { digest: string; result: ManagedSequenceMutationResult }
  >();
  const prepares: string[] = [];
  const effects: string[] = [];
  const result = (): ManagedSequenceMutationResult => ({
    schema: "h3.context.managed_sequence_result.v1",
    parentSequenceId: "managed.recovery.1",
    authorizationFingerprint: fp("1"),
    readAuthorityFingerprint: fp("2"),
    state,
    revision,
    execution: null,
    childAuthority: null,
    contextWorkspaceHandle: null,
    parentExpiresAtEpochMs: 87_400_000,
    replayed: false,
  });
  const receive = (
    id: string,
    action: string,
    payload: Record<string, unknown>,
  ): ManagedSequenceMutationResult => {
    const digest = JSON.stringify({ action, payload });
    const previous = ledger.get(id);
    // Match the real service's ordering: identity replay/conflict precedes revision CAS.
    if (previous !== undefined) {
      if (previous.digest !== digest) throw new Error("request_id_conflict");
      return { ...previous.result, replayed: true };
    }
    if (action !== "authorize_sequence")
      expect(payload.expected_revision).toBe(revision);
    effects.push(action);
    let next: ManagedSequenceMutationResult;
    switch (action) {
      case "authorize_sequence":
        next = result();
        break;
      case "start_sequence":
        state = "active";
        revision += 1;
        next = result();
        break;
      case "prepare_sequence_child":
        expect(state).toBe("active");
        prepares.push(id);
        revision += 1;
        next = {
          ...result(),
          contextWorkspaceHandle: `ws_${"c".repeat(40)}`,
          execution: {
            parentSequenceId: "managed.recovery.1",
            parentAuthorizationFingerprint: fp("1"),
            segmentId: "segment.1",
            slotRevision: revision,
            attemptEpoch: attempt,
            materializationReceiptFingerprint: fp("3"),
            predecessorTerminalFingerprint: null,
            requestId: id,
            jobCount: 1,
            fingerprint: fp(attempt === 1 ? "4" : "5"),
          },
        };
        break;
      case "fail_prepared_child":
        state = "paused_failure";
        revision += 1;
        next = result();
        break;
      case "retry_segment":
        expect(state).toBe("paused_failure");
        expect(attempt).toBe(1);
        state = "active";
        attempt = 2;
        revision += 1;
        next = result();
        break;
      case "bind_prepared_child":
        revision += 1;
        next = {
          ...result(),
          childAuthority: {
            schema: "h3.context.managed_sequence_child_authority.v1",
            runHandle: `mc_${"d".repeat(40)}`,
            stateFingerprint: fp("6"),
            jobId: "job.retry.1",
            transactionId: "transaction.retry.1",
          },
        };
        break;
      case "record_submission":
        revision += 2;
        next = result();
        break;
      default:
        throw new Error(`unexpected action ${action}`);
    }
    ledger.set(id, { digest, result: next });
    return next;
  };
  const parentClient = {
    read: vi.fn(async () => {
      throw new Error("unexpected read");
    }),
    send: vi.fn(
      async (id: string, action: string, payload: Record<string, unknown>) => {
        const first = receive(id, action, payload);
        const afterFirst = revision;
        expect(receive(id, action, payload)).toEqual({
          ...first,
          replayed: true,
        });
        expect(revision).toBe(afterFirst);
        return first;
      },
    ),
  };
  const workflowAuthority = {};
  let resolved = 0;
  let queueCalls = 0;
  const preflight: ManagedAppModePreflight = {
    bootstrap: { output: {}, workflow: {} },
    observation: {
      schema: "h3.context.prepared_graph_observation.v4",
      route: "existing",
      graph_fingerprint: fp("7"),
      compiled_prompt_fingerprint: fp("8"),
      owned_projection_fingerprint: fp("9"),
      owned_node_ids: ["1"],
      owned_link_ids: [],
      model_fingerprint: fp("a"),
      runtime_fingerprint: fp("b"),
      fingerprint_domain: "output_producing_graph",
      expected_frames: 100,
      source_identity: null,
      timeout_ms: 60_000,
      native_anchor_node_id: "native.1",
    },
    productShellNodeId: "product.1",
    nativeAnchorNodeId: "native.1",
  };
  const runner = createManagedSerialSequenceRunner({
    parentClient,
    coordinatorClient: {
      send: vi.fn(
        async () =>
          ({
            sequence: { state_fingerprint: fp("c") },
          }) as SequenceCoordinatorResult,
      ),
    },
    reattachStore: {
      read: () => undefined,
      replace: () => true,
      clear: () => undefined,
    },
    resolveChild: async (execution) => {
      resolved += 1;
      expect(execution.attemptEpoch).toBe(resolved);
      if (resolved === 1) throw new Error("synthetic compile refusal");
      return {
        inputs: {
          task_mode: "t2va",
          user_intent: "synthetic",
          duration_milliseconds: 4_000,
          frame_count: 100,
        },
        workflowAuthority,
        activeWorkflowFingerprint: fp("d"),
        previousOwnedProjectionFingerprint: fp("e"),
      };
    },
    startChild: async (_inputs, options = {}) => {
      const authority = await options.prepareManaged!(preflight);
      authority.bindCanvasIdentity?.({
        workflowAuthority,
        ownedReference: {
          nodeIds: ["1"],
          linkIds: [],
          anchorNodeId: "1",
          authoredWidgetNodeIds: [],
        },
      });
      queueCalls += 1;
      authority.onQueueSubmitted();
      const accepted: AppModeStartResult = {
        queueResult: {
          prompt_id: "prompt.retry.1",
          number: 1,
          nodeErrorClassTypes: [],
          nodeErrorTypes: [],
        },
        graphFingerprint: fp("7"),
        compiledPromptFingerprint: fp("8"),
        ownedProjectionFingerprint: fp("9"),
        ownedNodeIds: ["1"],
        ownedLinkIds: [],
        queuePromptId: "prompt.retry.1",
        route: "existing",
      };
      await authority.onQueueAccepted(accepted);
      return accepted;
    },
  });
  await expect(
    runner.start({
      authorization: {
        workspace_handle: `pw_${"w".repeat(40)}`,
        expected_workspace_revision: 1,
        expected_workspace_fingerprint: fp("a"),
        expected_plan_fingerprint: fp("b"),
        generation_plan_fingerprint: fp("c"),
        compiler_fingerprint: fp("d"),
        host_capability_fingerprint: fp("e"),
        explicit_intent: "generate_approved_sequence",
      },
      qualificationFingerprint: fp("f"),
      segmentIds: ["segment.1"],
    }),
  ).rejects.toThrow("synthetic compile refusal");
  expect(queueCalls).toBe(0);
  expect(runner.snapshot().parentState).toBe("paused_failure");
  await runner.retry({
    segmentId: "segment.1",
    activeWorkflowFingerprint: fp("d"),
    ownedProjectionFingerprint: fp("e"),
  });
  expect(prepares).toHaveLength(2);
  expect(new Set(prepares).size).toBe(2);
  expect(resolved).toBe(2);
  expect(queueCalls).toBe(1);
  expect(
    effects.filter((action) => action === "record_submission"),
  ).toHaveLength(1);
  expect(runner.snapshot()).toMatchObject({
    parentState: "active",
    activeQueuePromptId: "prompt.retry.1",
  });
});
