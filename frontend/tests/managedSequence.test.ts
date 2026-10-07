import { describe, expect, it, vi } from "vitest";

import {
  MANAGED_SEQUENCE_ACTION_SCHEMA,
  MANAGED_SEQUENCE_REATTACH_STORAGE_KEY,
  createBrowserManagedSequenceReattachStore,
  createManagedSequenceClient,
  createManagedSequenceReattachStore,
  createManagedSerialSequenceRunner,
  decodeManagedSequenceMutationResult,
  decodeManagedSequenceProjection,
  type EligibleSegmentExecution,
  type ManagedSequenceMutationResult,
  type ManagedSequenceProjection,
} from "../src/host/managedSequence";
import type {
  AppModeInputs,
  AppModeStartOptions,
  AppModeStartResult,
  ManagedAppModePreflight,
} from "../src/host/appMode";
import type { SequenceCoordinatorResult } from "../src/host/sequenceCoordinator";

const fingerprint = (digit: string) => `sha256:${digit.repeat(64)}`;
const parentSequenceId = "managed.sequence.1";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

function mutationWire() {
  return {
    schema: "h3.context.managed_sequence_result.v1",
    parent_sequence_id: parentSequenceId,
    authorization_fingerprint: fingerprint("1"),
    read_authority_fingerprint: fingerprint("2"),
    state: "active",
    revision: 1,
    execution: null,
    execution_fingerprint: null,
    child_authority: null,
    context_workspace_handle: null,
    parent_expires_at_epoch_ms: 87_400_000,
    replayed: false,
  };
}

function projectionWire() {
  return {
    schema: "h3.context.managed_sequence_projection.v1",
    parent_sequence_id: parentSequenceId,
    authorization_fingerprint: fingerprint("1"),
    state: "active",
    revision: 1,
    etag: fingerprint("3"),
    lease: {
      schema: "h3.context.managed_sequence_lease.v1",
      created_at: "4059000000000000",
      idle_deadline: "409f400000000000",
      active_deadline: null,
      absolute_deadline: "40f51f4000000000",
      idle_expiry_handled: false,
    },
    active_segment_id: null,
    slots: [
      {
        schema: "h3.context.managed_sequence_slot.v1",
        segment_id: "segment.1",
        ordinal: 1,
        state: "eligible",
        attempt_epoch: 1,
        eligible_execution_fingerprint: null,
        child_run_handle: null,
        child_state_fingerprint: null,
        graph_fingerprint: null,
        compiled_prompt_fingerprint: null,
        canvas_write_fingerprint: null,
        queue_prompt_id: null,
        artifact_receipt_fingerprint: null,
        terminal_fingerprint: null,
      },
    ],
  };
}

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  };
}

function mutationResult(
  revision: number,
  state:
    | "authorized"
    | "active"
    | "paused_client_absent"
    | "paused_failure"
    | "paused_unknown_ownership"
    | "cancelled"
    | "succeeded",
  execution: EligibleSegmentExecution | null = null,
  childAuthority: ManagedSequenceMutationResult["childAuthority"] = null,
): ManagedSequenceMutationResult {
  return Object.freeze({
    schema: "h3.context.managed_sequence_result.v1",
    parentSequenceId,
    authorizationFingerprint: fingerprint("1"),
    readAuthorityFingerprint: fingerprint("2"),
    state,
    revision,
    execution,
    childAuthority,
    contextWorkspaceHandle: execution === null ? null : `ws_${"c".repeat(40)}`,
    parentExpiresAtEpochMs: 87_400_000,
    replayed: false,
  });
}

function execution(
  segmentId: string,
  revision: number,
  predecessorTerminalFingerprint: string | null = null,
): EligibleSegmentExecution {
  return Object.freeze({
    parentSequenceId,
    parentAuthorizationFingerprint: fingerprint("1"),
    segmentId,
    slotRevision: revision,
    attemptEpoch: 1,
    materializationReceiptFingerprint: fingerprint("3"),
    predecessorTerminalFingerprint,
    requestId: `prepare.${segmentId}`,
    jobCount: 1,
    fingerprint: fingerprint("4"),
  });
}

function detachedProjection(
  firstState: "submitted" | "artifact_verified" = "submitted",
): ManagedSequenceProjection {
  return Object.freeze({
    schema: "h3.context.managed_sequence_projection.v1",
    parentSequenceId,
    authorizationFingerprint: fingerprint("1"),
    state: "paused_client_absent",
    revision: 6,
    etag: fingerprint("6"),
    lease: Object.freeze({
      createdAt: "4059000000000000",
      idleDeadline: "409f400000000000",
      activeDeadline: "40b3880000000000",
      absoluteDeadline: "40f51f4000000000",
      idleExpiryHandled: false,
    }),
    activeSegmentId: "segment.1",
    slots: Object.freeze([
      Object.freeze({
        segmentId: "segment.1",
        ordinal: 1,
        state: firstState,
        attemptEpoch: 1,
        eligibleExecutionFingerprint: fingerprint("4"),
        childRunHandle: `mc_${"1".repeat(40)}`,
        childStateFingerprint: fingerprint("5"),
        graphFingerprint: fingerprint("6"),
        compiledPromptFingerprint: fingerprint("7"),
        canvasWriteFingerprint: fingerprint("8"),
        queuePromptId: "prompt.1",
        artifactReceiptFingerprint:
          firstState === "artifact_verified" ? fingerprint("a") : null,
        terminalFingerprint: null,
      }),
      Object.freeze({
        segmentId: "segment.2",
        ordinal: 2,
        state: "pending",
        attemptEpoch: 1,
        eligibleExecutionFingerprint: null,
        childRunHandle: null,
        childStateFingerprint: null,
        graphFingerprint: null,
        compiledPromptFingerprint: null,
        canvasWriteFingerprint: null,
        queuePromptId: null,
        artifactReceiptFingerprint: null,
        terminalFingerprint: null,
      }),
    ]),
  });
}

function detachedFailureProjection(): ManagedSequenceProjection {
  const projection = detachedProjection();
  return Object.freeze({
    ...projection,
    state: "paused_failure" as const,
    activeSegmentId: null,
    slots: Object.freeze([
      Object.freeze({
        ...projection.slots[0]!,
        state: "failed",
        terminalFingerprint: fingerprint("f"),
      }),
      projection.slots[1]!,
    ]),
  });
}

function coordinatorResult(
  run: string,
  stateFingerprint: string,
  disposition: SequenceCoordinatorResult["disposition"],
  artifactAuthority: SequenceCoordinatorResult["artifactAuthority"] = null,
  terminalFingerprint: string | null = null,
): SequenceCoordinatorResult {
  return {
    schema: "h3.context.generation_coordinator.response.v1",
    runHandle: run,
    disposition,
    artifactAuthority,
    terminalFingerprint,
    sequence: { state_fingerprint: stateFingerprint },
    production: {},
  } as unknown as SequenceCoordinatorResult;
}

function preflight(
  ownedProjectionFingerprint: string,
): ManagedAppModePreflight {
  return {
    bootstrap: { output: {}, workflow: {} },
    observation: {
      schema: "h3.context.prepared_graph_observation.v4",
      route: "existing",
      graph_fingerprint: fingerprint("5"),
      compiled_prompt_fingerprint: fingerprint("6"),
      owned_projection_fingerprint: ownedProjectionFingerprint,
      owned_node_ids: ["1"],
      owned_link_ids: [],
      model_fingerprint: fingerprint("7"),
      runtime_fingerprint: fingerprint("8"),
      fingerprint_domain: "output_producing_graph",
      expected_frames: 192,
      source_identity: null,
      timeout_ms: 60_000,
      native_anchor_node_id: "native.1",
    },
    productShellNodeId: "product.shell.1",
    nativeAnchorNodeId: "native.1",
  };
}

const startIntent = Object.freeze({
  authorization: {
    workspace_handle: `pw_${"w".repeat(40)}`,
    expected_workspace_revision: 1,
    expected_workspace_fingerprint: fingerprint("a"),
    expected_plan_fingerprint: fingerprint("b"),
    generation_plan_fingerprint: fingerprint("c"),
    compiler_fingerprint: fingerprint("d"),
    host_capability_fingerprint: fingerprint("e"),
    explicit_intent: "generate_approved_sequence" as const,
  },
  qualificationFingerprint: fingerprint("f"),
  segmentIds: ["segment.1", "segment.2"],
});

describe("managed parent client", () => {
  it("decodes the exact child coordinator authority only on a bound result", () => {
    const wire = {
      ...mutationWire(),
      child_authority: {
        schema: "h3.context.managed_sequence_child_authority.v1",
        run_handle: "mc_" + "d".repeat(40),
        state_fingerprint: fingerprint("4"),
        job_id: "job.segment.1",
        transaction_id: "transaction.segment.1",
      },
    };

    expect(decodeManagedSequenceMutationResult(wire).childAuthority).toEqual({
      schema: "h3.context.managed_sequence_child_authority.v1",
      runHandle: "mc_" + "d".repeat(40),
      stateFingerprint: fingerprint("4"),
      jobId: "job.segment.1",
      transactionId: "transaction.segment.1",
    });
  });

  it("sends one closed mutation action through the dedicated route", async () => {
    const fetchApi = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => mutationWire(),
    }));
    const client = createManagedSequenceClient({ fetchApi });

    await expect(
      client.send("start.sequence.1", "start_sequence", {
        parent_sequence_id: parentSequenceId,
        expected_revision: 0,
        authorization_fingerprint: fingerprint("1"),
      }),
    ).resolves.toMatchObject({
      parentSequenceId,
      state: "active",
      revision: 1,
      parentExpiresAtEpochMs: 87_400_000,
    });
    const firstCall = fetchApi.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    const body = JSON.parse(firstCall[1].body as string);
    expect(body).toEqual({
      schema: MANAGED_SEQUENCE_ACTION_SCHEMA,
      request_id: "start.sequence.1",
      action: "start_sequence",
      payload: {
        parent_sequence_id: parentSequenceId,
        expected_revision: 0,
        authorization_fingerprint: fingerprint("1"),
      },
    });
  });

  it("reads current progress without a request id and honors ETag 304", async () => {
    const fetchApi = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: {
          get: (name: string) => (name === "etag" ? fingerprint("3") : null),
        },
        json: async () => projectionWire(),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 304,
        headers: {
          get: (name: string) => (name === "etag" ? fingerprint("3") : null),
        },
        json: vi.fn(),
      });
    const client = createManagedSequenceClient({ fetchApi });

    const current = await client.read(parentSequenceId, fingerprint("2"));
    const unchanged = await client.read(
      parentSequenceId,
      fingerprint("2"),
      current.etag,
    );

    expect(current).toMatchObject({ status: 200, etag: fingerprint("3") });
    expect(current.projection?.lease.idleExpiryHandled).toBe(false);
    expect(unchanged).toEqual({
      status: 304,
      etag: fingerprint("3"),
      projection: null,
    });
    expect(fetchApi.mock.calls[0][0]).toContain(
      encodeURIComponent(parentSequenceId),
    );
    expect(fetchApi.mock.calls[0][1]).toMatchObject({ method: "GET" });
    expect(fetchApi.mock.calls[0][1]).not.toHaveProperty("body");
    expect(fetchApi.mock.calls[1][1].headers).toMatchObject({
      "if-none-match": fingerprint("3"),
    });
  });

  it("rejects a projection whose ordered slots or active child are contradictory", () => {
    const wrongOrdinal = projectionWire();
    wrongOrdinal.slots[0]!.ordinal = 2;
    expect(() => decodeManagedSequenceProjection(wrongOrdinal)).toThrow(
      "invalid_response",
    );

    expect(() =>
      decodeManagedSequenceProjection({
        ...projectionWire(),
        active_segment_id: "segment.foreign",
      }),
    ).toThrow("invalid_response");
  });
});

describe("one bounded managed-sequence reattach pointer", () => {
  it("keeps browser persistence behind the host adapter and preserves foreign keys", () => {
    const foreignKey = "host.foreign.preference";
    globalThis.localStorage.removeItem(MANAGED_SEQUENCE_REATTACH_STORAGE_KEY);
    globalThis.localStorage.setItem(foreignKey, "preserve");

    try {
      const store = createBrowserManagedSequenceReattachStore(() => 1_000);
      expect(
        store.replace({
          parentSequenceId,
          readAuthorityFingerprint: fingerprint("2"),
          expiresAtEpochMs: 10_000,
        }),
      ).toBe(true);
      expect(store.read()?.parentSequenceId).toBe(parentSequenceId);

      store.clear();
      expect(
        globalThis.localStorage.getItem(MANAGED_SEQUENCE_REATTACH_STORAGE_KEY),
      ).toBeNull();
      expect(globalThis.localStorage.getItem(foreignKey)).toBe("preserve");
    } finally {
      globalThis.localStorage.removeItem(MANAGED_SEQUENCE_REATTACH_STORAGE_KEY);
      globalThis.localStorage.removeItem(foreignKey);
    }
  });

  it("replaces the only pointer and round-trips no mutation authority", () => {
    const storage = memoryStorage();
    const store = createManagedSequenceReattachStore(storage, () => 1_000);

    store.replace({
      parentSequenceId,
      readAuthorityFingerprint: fingerprint("2"),
      expiresAtEpochMs: 10_000,
    });
    store.replace({
      parentSequenceId: "managed.sequence.2",
      readAuthorityFingerprint: fingerprint("4"),
      expiresAtEpochMs: 20_000,
    });

    expect(storage.length).toBe(1);
    expect(store.read()).toEqual({
      schema: "h3.context.managed_sequence_reattach_pointer.v1",
      parentSequenceId: "managed.sequence.2",
      readAuthorityFingerprint: fingerprint("4"),
      expiresAtEpochMs: 20_000,
    });
    const serialized =
      storage.getItem(MANAGED_SEQUENCE_REATTACH_STORAGE_KEY) ?? "";
    expect(serialized).not.toMatch(/request|resume|cancel|retry|queue|prompt/i);
  });

  it("removes corrupt or expired pointers without guessing a backend parent", () => {
    const storage = memoryStorage();
    const store = createManagedSequenceReattachStore(storage, () => 10_000);
    storage.setItem(MANAGED_SEQUENCE_REATTACH_STORAGE_KEY, "{not-json");
    expect(store.read()).toBeUndefined();
    expect(storage.length).toBe(0);

    expect(
      store.replace({
        parentSequenceId,
        readAuthorityFingerprint: fingerprint("2"),
        expiresAtEpochMs: 9_999,
      }),
    ).toBe(false);
    expect(store.read()).toBeUndefined();
    expect(storage.length).toBe(0);
  });

  it("stores the pointer synchronously before a first queue effect can run", async () => {
    const storage = memoryStorage();
    const store = createManagedSequenceReattachStore(storage, () => 1_000);
    const events: string[] = [];

    store.replace({
      parentSequenceId,
      readAuthorityFingerprint: fingerprint("2"),
      expiresAtEpochMs: 10_000,
    });
    await Promise.resolve().then(() => {
      expect(store.read()).toBeDefined();
      events.push("queue");
    });

    expect(events).toEqual(["queue"]);
  });
});

describe("managed serial sequence runner", () => {
  it.each(["running-first", "artifact-first", "terminal-first"] as const)(
    "keeps monotonic child progress for %s observations and releases before its successor",
    async (order) => {
      const events: string[] = [];
      const storage = memoryStorage();
      const reattachStore = createManagedSequenceReattachStore(
        storage,
        () => 1_000,
      );
      let revision = -1;
      let activeSegment = "segment.1";
      let childNumber = 0;
      let childState = fingerprint("1");
      const artifactSeen = new Set<number>();
      const terminalSeen = new Set<number>();
      let parentChildState = "prepared";
      const parentClient = {
        send: vi.fn(
          async (
            _requestId: string,
            action: string,
            payload: Record<string, unknown>,
          ) => {
            events.push(`parent:${action}`);
            if (action === "authorize_sequence") {
              revision = 0;
              return mutationResult(revision, "authorized");
            }
            expect(payload.expected_revision).toBe(revision);
            if (action === "start_sequence") {
              revision = 1;
              return mutationResult(revision, "active");
            }
            if (action === "prepare_sequence_child") {
              parentChildState = "prepared";
              activeSegment = payload.segment_id as string;
              revision += 1;
              return mutationResult(
                revision,
                "active",
                execution(
                  activeSegment,
                  revision,
                  payload.predecessor_terminal_fingerprint as string | null,
                ),
              );
            }
            if (action === "bind_prepared_child") {
              childNumber += 1;
              revision += 1;
              childState = fingerprint(String(childNumber + 1));
              return mutationResult(revision, "active", null, {
                schema: "h3.context.managed_sequence_child_authority.v1",
                runHandle: `mc_${String(childNumber).repeat(40)}`,
                stateFingerprint: childState,
                jobId: `job.${childNumber}`,
                transactionId: `transaction.${childNumber}`,
              });
            }
            if (action === "record_submission") {
              parentChildState = "submitted";
              revision += 2;
              return mutationResult(revision, "active");
            }
            if (action === "record_running") {
              // Match the real parent FSM: output authority may advance straight from submitted.
              if (
                parentChildState !== "submitted" &&
                parentChildState !== "running"
              )
                throw new Error("invalid_child_transition");
              parentChildState = "running";
              revision += 1;
              return mutationResult(revision, "active");
            }
            if (action === "record_artifact") {
              expect(["submitted", "running"]).toContain(parentChildState);
              parentChildState = "artifact_verified";
              revision += 1;
              return mutationResult(revision, "active");
            }
            if (action === "record_terminal") {
              expect(parentChildState).toBe("artifact_verified");
              parentChildState = "succeeded";
              revision += 1;
              return mutationResult(
                revision,
                activeSegment === "segment.2" ? "succeeded" : "active",
              );
            }
            throw new Error(`unexpected parent action ${action}`);
          },
        ),
        read: vi.fn(),
      };
      const coordinatorClient = {
        send: vi.fn(
          async (
            _requestId: string,
            action: string,
            payload: Record<string, unknown>,
          ) => {
            events.push(`child:${action}`);
            const run = payload.run_handle as string;
            if (action === "record_submission") {
              childState = fingerprint(String(childNumber + 3));
              return coordinatorResult(run, childState, "submitted");
            }
            if (action === "record_running") {
              if (terminalSeen.has(childNumber))
                throw new Error("invalid_generation_transition");
              childState = fingerprint(String(childNumber + 4));
              return coordinatorResult(run, childState, "running");
            }
            if (action === "record_artifact") {
              artifactSeen.add(childNumber);
              childState = fingerprint(String(childNumber + 5));
              return coordinatorResult(
                run,
                childState,
                terminalSeen.has(childNumber)
                  ? "succeeded"
                  : "artifact_verified",
                {
                  schema:
                    "h3.context.generation_coordinator.artifact_authority.v1",
                  receiptFingerprint: fingerprint("a"),
                  byteLength: 23,
                },
                terminalSeen.has(childNumber) ? fingerprint("b") : null,
              );
            }
            if (action === "record_terminal") {
              terminalSeen.add(childNumber);
              childState = fingerprint(String(childNumber + 7));
              return coordinatorResult(
                run,
                childState,
                artifactSeen.has(childNumber)
                  ? "succeeded"
                  : "verification_pending",
                artifactSeen.has(childNumber)
                  ? {
                      schema:
                        "h3.context.generation_coordinator.artifact_authority.v1",
                      receiptFingerprint: fingerprint("a"),
                      byteLength: 23,
                    }
                  : null,
                artifactSeen.has(childNumber) ? fingerprint("b") : null,
              );
            }
            if (action === "release_sequence")
              return coordinatorResult(run, childState, "released");
            throw new Error(`unexpected coordinator action ${action}`);
          },
        ),
      };
      const workflowAuthority = {};
      let queueCalls = 0;
      const startChild = vi.fn(
        async (
          _inputs: AppModeInputs,
          options: AppModeStartOptions = {},
        ): Promise<AppModeStartResult> => {
          const owned = fingerprint(String(childNumber + 8));
          const prepared = preflight(owned);
          const authority = await options.prepareManaged!(prepared);
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
          events.push(`queue:${activeSegment}`);
          authority.onQueueSubmitted();
          const result: AppModeStartResult = {
            queueResult: {
              prompt_id: `prompt.${childNumber}`,
              number: childNumber,
              nodeErrorClassTypes: Object.freeze([]),
              nodeErrorTypes: Object.freeze([]),
            },
            graphFingerprint: prepared.observation.graph_fingerprint,
            compiledPromptFingerprint:
              prepared.observation.compiled_prompt_fingerprint,
            ownedProjectionFingerprint: owned,
            ownedNodeIds: ["1"],
            ownedLinkIds: [],
            queuePromptId: `prompt.${childNumber}`,
            route: "existing",
          };
          await authority.onQueueAccepted(result);
          return result;
        },
      );
      const runner = createManagedSerialSequenceRunner({
        parentClient,
        coordinatorClient,
        reattachStore,
        startChild,
        resolveChild: async (_eligible, _context, index) => ({
          inputs: {
            task_mode: "t2va",
            user_intent: "synthetic",
            duration_milliseconds: 8_000,
            frame_count: 192,
          },
          options: { useExisting: true },
          workflowAuthority,
          activeWorkflowFingerprint: fingerprint("9"),
          previousOwnedProjectionFingerprint:
            index === 0 ? fingerprint("0") : fingerprint("9"),
        }),
      });

      expect(queueCalls).toBe(0);
      expect(runner.canvasLabel()).toBeNull();
      await runner.start(startIntent);
      expect(queueCalls).toBe(1);
      expect(reattachStore.read()?.parentSequenceId).toBe(parentSequenceId);
      // The bound label is the one an explicit resume or retry must present again.
      expect(runner.canvasLabel()).toEqual({
        workflowAuthority,
        activeWorkflowFingerprint: fingerprint("9"),
      });
      expect(runner.canvasLabel()?.workflowAuthority).toBe(workflowAuthority);

      await runner.recordRunning("prompt.foreign");
      await runner.recordArtifact({
        promptId: "prompt.foreign",
        outputNodeId: "save.video",
        locator: { filename: "foreign.mp4", subfolder: "", type: "output" },
      });
      await runner.recordTerminal({
        promptId: "prompt.foreign",
        kind: "success",
      });
      const artifact = () =>
        runner.recordArtifact({
          promptId: "prompt.1",
          outputNodeId: "save.video",
          locator: { filename: "managed.mp4", subfolder: "", type: "output" },
        });
      const terminal = () =>
        runner.recordTerminal({ promptId: "prompt.1", kind: "success" });
      const first =
        order === "artifact-first"
          ? artifact()
          : order === "terminal-first"
            ? terminal()
            : runner.recordRunning("prompt.1");
      // Queue observations while the earlier asynchronous publication is still pending.
      const lateRunning = runner.recordRunning("prompt.1");
      const repeatedRunning = runner.recordRunning("prompt.1");
      await Promise.all([first, lateRunning, repeatedRunning]);
      expect(
        coordinatorClient.send.mock.calls.filter(
          ([, action]) => action === "record_running",
        ),
      ).toHaveLength(order === "running-first" ? 1 : 0);
      expect(
        parentClient.send.mock.calls.filter(
          ([, action]) => action === "record_running",
        ),
      ).toHaveLength(order === "running-first" ? 1 : 0);

      expect(queueCalls).toBe(1);
      if (order !== "artifact-first") await artifact();
      if (order !== "terminal-first") await terminal();

      expect(queueCalls).toBe(2);
      expect(events.indexOf("child:release_sequence")).toBeLessThan(
        events.indexOf("parent:record_terminal"),
      );
      expect(events.indexOf("parent:record_terminal")).toBeLessThan(
        events.indexOf("queue:segment.2"),
      );

      const beforeStaleRunning = events.length;
      await runner.recordRunning("prompt.1");
      expect(events).toHaveLength(beforeStaleRunning);
      await runner.recordRunning("prompt.2");
      await runner.recordRunning("prompt.2");
      expect(events.slice(beforeStaleRunning)).toEqual([
        "child:record_running",
        "parent:record_running",
      ]);
      await runner.recordTerminal({ promptId: "prompt.2", kind: "success" });
      expect(queueCalls).toBe(2);
      await runner.recordArtifact({
        promptId: "prompt.2",
        outputNodeId: "save.video",
        locator: { filename: "managed-2.mp4", subfolder: "", type: "output" },
      });
      expect(queueCalls).toBe(2);
      expect(runner.snapshot()).toMatchObject({
        attached: true,
        parentState: "succeeded",
        activeSegmentId: null,
      });
    },
  );

  it("records an ambiguous invocation once and never schedules another child", async () => {
    let revision = -1;
    let queueCalls = 0;
    const actions: string[] = [];
    const parentClient = {
      send: vi.fn(async (_id: string, action: string) => {
        actions.push(action);
        if (action === "authorize_sequence") {
          revision = 0;
          return mutationResult(revision, "authorized");
        }
        if (action === "start_sequence") {
          revision = 1;
          return mutationResult(revision, "active");
        }
        if (action === "prepare_sequence_child") {
          revision = 2;
          return mutationResult(
            revision,
            "active",
            execution("segment.1", revision),
          );
        }
        if (action === "bind_prepared_child") {
          revision = 3;
          return mutationResult(revision, "active", null, {
            schema: "h3.context.managed_sequence_child_authority.v1",
            runHandle: `mc_${"1".repeat(40)}`,
            stateFingerprint: fingerprint("4"),
            jobId: "job.1",
            transactionId: "transaction.1",
          });
        }
        if (action === "mark_invocation_unknown") {
          revision = 5;
          return mutationResult(revision, "paused_unknown_ownership");
        }
        throw new Error(`unexpected action ${action}`);
      }),
      read: vi.fn(),
    };
    const workflowAuthority = {};
    const runner = createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient: { send: vi.fn() },
      reattachStore: createManagedSequenceReattachStore(
        memoryStorage(),
        () => 1_000,
      ),
      startChild: async (_inputs, options = {}) => {
        const prepared = preflight(fingerprint("9"));
        const authority = await options.prepareManaged!(prepared);
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
        await authority.onQueueFailed("ambiguous");
        throw new Error("ambiguous queue acknowledgement");
      },
      resolveChild: async () => ({
        inputs: {
          task_mode: "t2va",
          user_intent: "synthetic",
          duration_milliseconds: 8_000,
          frame_count: 192,
        },
        workflowAuthority,
        activeWorkflowFingerprint: fingerprint("8"),
        previousOwnedProjectionFingerprint: fingerprint("7"),
      }),
    });

    await expect(runner.start(startIntent)).rejects.toThrow(
      "ambiguous queue acknowledgement",
    );
    expect(queueCalls).toBe(1);
    expect(
      actions.filter((action) => action === "mark_invocation_unknown"),
    ).toHaveLength(1);
    expect(actions).not.toContain("record_submission");
    expect(runner.snapshot()).toMatchObject({
      parentState: "paused_unknown_ownership",
      activeSegmentId: "segment.1",
    });
  });

  it("records exact reuse before child preparation and queues only the dirty successor", async () => {
    let revision = -1;
    const actions: string[] = [];
    const workflowAuthority = {};
    const parentClient = {
      send: vi.fn(
        async (
          _id: string,
          action: string,
          payload: Record<string, unknown>,
        ) => {
          actions.push(action);
          if (action === "authorize_sequence") {
            revision = 0;
            return mutationResult(revision, "authorized");
          }
          if (action === "start_sequence") {
            revision = 1;
            return mutationResult(revision, "active");
          }
          if (action === "record_reuse") {
            expect(payload).toMatchObject({
              segment_id: "segment.1",
              artifact_receipt_fingerprint: fingerprint("a"),
              artifact_bytes: 23,
              terminal_fingerprint: fingerprint("b"),
            });
            revision = 2;
            return mutationResult(revision, "active");
          }
          if (action === "prepare_sequence_child") {
            expect(payload).toMatchObject({
              segment_id: "segment.2",
              predecessor_terminal_fingerprint: fingerprint("b"),
            });
            revision = 3;
            return mutationResult(
              revision,
              "active",
              execution("segment.2", revision, fingerprint("b")),
            );
          }
          if (action === "bind_prepared_child") {
            revision = 4;
            return mutationResult(revision, "active", null, {
              schema: "h3.context.managed_sequence_child_authority.v1",
              runHandle: `mc_${"2".repeat(40)}`,
              stateFingerprint: fingerprint("c"),
              jobId: "job.2",
              transactionId: "transaction.2",
            });
          }
          if (action === "record_submission") {
            revision = 6;
            return mutationResult(revision, "active");
          }
          throw new Error(`unexpected parent action ${action}`);
        },
      ),
      read: vi.fn(),
    };
    const coordinatorClient = {
      send: vi.fn(
        async (
          _id: string,
          action: string,
          payload: Record<string, unknown>,
        ) => {
          if (action === "record_submission")
            return coordinatorResult(
              payload.run_handle as string,
              fingerprint("d"),
              "submitted",
            );
          throw new Error(`unexpected coordinator action ${action}`);
        },
      ),
    };
    let queueCalls = 0;
    const startChild = vi.fn(
      async (
        _inputs: AppModeInputs,
        options: AppModeStartOptions = {},
      ): Promise<AppModeStartResult> => {
        const prepared = preflight(fingerprint("0"));
        const queueAuthority = await options.prepareManaged!(prepared);
        queueAuthority.bindCanvasIdentity?.({
          workflowAuthority,
          ownedReference: {
            nodeIds: ["1"],
            linkIds: [],
            anchorNodeId: "1",
            authoredWidgetNodeIds: [],
          },
        });
        queueCalls += 1;
        queueAuthority.onQueueSubmitted();
        const result: AppModeStartResult = {
          queueResult: {
            prompt_id: "prompt.2",
            number: 2,
            nodeErrorClassTypes: Object.freeze([]),
            nodeErrorTypes: Object.freeze([]),
          },
          graphFingerprint: prepared.observation.graph_fingerprint,
          compiledPromptFingerprint:
            prepared.observation.compiled_prompt_fingerprint,
          ownedProjectionFingerprint: fingerprint("0"),
          ownedNodeIds: ["1"],
          ownedLinkIds: [],
          queuePromptId: "prompt.2",
          route: "existing",
        };
        await queueAuthority.onQueueAccepted(result);
        return result;
      },
    );
    const resolveChild = vi.fn(async () => ({
      inputs: {
        task_mode: "t2va" as const,
        user_intent: "synthetic",
        duration_milliseconds: 8_000,
        frame_count: 192,
      },
      workflowAuthority,
      activeWorkflowFingerprint: fingerprint("8"),
      previousOwnedProjectionFingerprint: fingerprint("7"),
    }));
    const runner = createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient,
      reattachStore: createManagedSequenceReattachStore(
        memoryStorage(),
        () => 1_000,
      ),
      startChild,
      resolveChild,
      resolveReuse: vi.fn(async (_segmentId, index) =>
        index === 0
          ? {
              artifactReceiptFingerprint: fingerprint("a"),
              byteLength: 23,
              terminalFingerprint: fingerprint("b"),
            }
          : null,
      ),
    });

    await runner.start(startIntent);

    expect(actions.indexOf("record_reuse")).toBeLessThan(
      actions.indexOf("prepare_sequence_child"),
    );
    expect(queueCalls).toBe(1);
    expect(resolveChild).toHaveBeenCalledTimes(1);
  });

  it("cancels successor effects while allowing the already host-owned child to terminalize", async () => {
    let revision = -1;
    let queueCalls = 0;
    const actions: string[] = [];
    const workflowAuthority = {};
    const parentClient = {
      send: vi.fn(
        async (
          _id: string,
          action: string,
          _payload: Record<string, unknown>,
        ) => {
          actions.push(action);
          if (action === "authorize_sequence") {
            revision = 0;
            return mutationResult(revision, "authorized");
          }
          if (action === "start_sequence") {
            revision = 1;
            return mutationResult(revision, "active");
          }
          if (action === "prepare_sequence_child") {
            revision = 2;
            return mutationResult(
              revision,
              "active",
              execution("segment.1", revision),
            );
          }
          if (action === "bind_prepared_child") {
            revision = 3;
            return mutationResult(revision, "active", null, {
              schema: "h3.context.managed_sequence_child_authority.v1",
              runHandle: `mc_${"1".repeat(40)}`,
              stateFingerprint: fingerprint("4"),
              jobId: "job.1",
              transactionId: "transaction.1",
            });
          }
          if (action === "record_submission") {
            revision = 5;
            return mutationResult(revision, "active");
          }
          if (action === "cancel_sequence") {
            revision = 6;
            return mutationResult(revision, "cancelled");
          }
          if (action === "record_terminal") {
            revision = 7;
            return mutationResult(revision, "cancelled");
          }
          throw new Error(`unexpected parent action ${action}`);
        },
      ),
      read: vi.fn(),
    };
    const coordinatorClient = {
      send: vi.fn(
        async (
          _id: string,
          action: string,
          payload: Record<string, unknown>,
        ) => {
          const run = payload.run_handle as string;
          if (action === "record_submission")
            return coordinatorResult(run, fingerprint("5"), "submitted");
          if (action === "record_terminal")
            return coordinatorResult(
              run,
              fingerprint("6"),
              "failed",
              null,
              fingerprint("7"),
            );
          if (action === "release_sequence")
            return coordinatorResult(run, fingerprint("6"), "released");
          throw new Error(`unexpected coordinator action ${action}`);
        },
      ),
    };
    const runner = createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient,
      reattachStore: createManagedSequenceReattachStore(
        memoryStorage(),
        () => 1_000,
      ),
      startChild: async (_inputs, options = {}) => {
        const prepared = preflight(fingerprint("9"));
        const queueAuthority = await options.prepareManaged!(prepared);
        queueAuthority.bindCanvasIdentity?.({
          workflowAuthority,
          ownedReference: {
            nodeIds: ["1"],
            linkIds: [],
            anchorNodeId: "1",
            authoredWidgetNodeIds: [],
          },
        });
        queueCalls += 1;
        queueAuthority.onQueueSubmitted();
        const result: AppModeStartResult = {
          queueResult: {
            prompt_id: "prompt.1",
            number: 1,
            nodeErrorClassTypes: Object.freeze([]),
            nodeErrorTypes: Object.freeze([]),
          },
          graphFingerprint: prepared.observation.graph_fingerprint,
          compiledPromptFingerprint:
            prepared.observation.compiled_prompt_fingerprint,
          ownedProjectionFingerprint: fingerprint("9"),
          ownedNodeIds: ["1"],
          ownedLinkIds: [],
          queuePromptId: "prompt.1",
          route: "existing",
        };
        await queueAuthority.onQueueAccepted(result);
        return result;
      },
      resolveChild: async () => ({
        inputs: {
          task_mode: "t2va",
          user_intent: "synthetic",
          duration_milliseconds: 8_000,
          frame_count: 192,
        },
        workflowAuthority,
        activeWorkflowFingerprint: fingerprint("8"),
        previousOwnedProjectionFingerprint: fingerprint("7"),
      }),
    });

    await runner.start(startIntent);
    await runner.cancel();
    await runner.recordTerminal({ promptId: "prompt.1", kind: "error" });

    expect(queueCalls).toBe(1);
    expect(actions).toContain("cancel_sequence");
    expect(
      actions.filter((action) => action === "prepare_sequence_child"),
    ).toHaveLength(1);
    expect(runner.snapshot()).toMatchObject({
      attached: false,
      parentState: "cancelled",
      activeSegmentId: null,
    });
  });

  it("fails a prepared child whose returned predecessor authority drifted before compile or queue", async () => {
    let revision = -1;
    const actions: string[] = [];
    const parentClient = {
      send: vi.fn(async (_id: string, action: string) => {
        actions.push(action);
        if (action === "authorize_sequence") {
          revision = 0;
          return mutationResult(revision, "authorized");
        }
        if (action === "start_sequence") {
          revision = 1;
          return mutationResult(revision, "active");
        }
        if (action === "prepare_sequence_child") {
          revision = 2;
          return mutationResult(revision, "active", {
            ...execution("segment.1", revision),
            predecessorTerminalFingerprint: fingerprint("9"),
          });
        }
        if (action === "fail_prepared_child") {
          revision = 3;
          return mutationResult(revision, "paused_failure");
        }
        throw new Error(`unexpected parent action ${action}`);
      }),
      read: vi.fn(),
    };
    const startChild = vi.fn();
    const resolveChild = vi.fn(async () => ({
      inputs: {
        task_mode: "t2va" as const,
        user_intent: "synthetic",
        duration_milliseconds: 8_000,
        frame_count: 192,
      },
      workflowAuthority: {},
      activeWorkflowFingerprint: fingerprint("8"),
      previousOwnedProjectionFingerprint: fingerprint("7"),
    }));
    const runner = createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient: { send: vi.fn() },
      reattachStore: createManagedSequenceReattachStore(
        memoryStorage(),
        () => 1_000,
      ),
      startChild,
      resolveChild,
    });

    await expect(runner.start(startIntent)).rejects.toMatchObject({
      code: "child_authority_mismatch",
    });
    expect(actions).toContain("fail_prepared_child");
    expect(resolveChild).not.toHaveBeenCalled();
    expect(startChild).not.toHaveBeenCalled();
  });

  it("reattaches read-first to only the exact retained prompt and performs no mutation or queue", async () => {
    const store = createManagedSequenceReattachStore(
      memoryStorage(),
      () => 1_000,
    );
    expect(
      store.replace({
        parentSequenceId,
        readAuthorityFingerprint: fingerprint("2"),
        expiresAtEpochMs: 10_000,
      }),
    ).toBe(true);
    const projection = detachedProjection();
    const events: string[] = [];
    const parentClient = {
      send: vi.fn(),
      read: vi.fn(async (parent: string, readAuthority: string) => {
        events.push("parent-read");
        expect(parent).toBe(parentSequenceId);
        expect(readAuthority).toBe(fingerprint("2"));
        return { status: 200 as const, etag: projection.etag, projection };
      }),
    };
    const historyClient = {
      read: vi.fn(async (promptId: string) => {
        events.push("history-read");
        expect(promptId).toBe("prompt.1");
        return {
          schema: "h3.context.comfy_prompt_history_observation.v1" as const,
          promptId,
          disposition: "not_yet_observed" as const,
          outputNodeIds: Object.freeze([]),
          outputIdentityFingerprint: null,
          terminalFingerprint: null,
        };
      }),
    };
    const startChild = vi.fn();
    const runner = createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient: { send: vi.fn() },
      historyClient,
      reattachStore: store,
      startChild,
      resolveChild: vi.fn(),
    });

    await expect(runner.reattach()).resolves.toMatchObject({
      disposition: "waiting_for_current_child",
      parentState: "paused_client_absent",
      activePromptId: "prompt.1",
    });
    expect(events).toEqual(["parent-read", "history-read"]);
    expect(parentClient.send).not.toHaveBeenCalled();
    expect(startChild).not.toHaveBeenCalled();
  });

  it("keeps a successful exact history row paused when no verified artifact authority was retained", async () => {
    const store = createManagedSequenceReattachStore(
      memoryStorage(),
      () => 1_000,
    );
    store.replace({
      parentSequenceId,
      readAuthorityFingerprint: fingerprint("2"),
      expiresAtEpochMs: 10_000,
    });
    const projection = detachedProjection();
    const parentClient = {
      send: vi.fn(),
      read: vi.fn(async () => ({
        status: 200 as const,
        etag: projection.etag,
        projection,
      })),
    };
    const coordinatorClient = { send: vi.fn() };
    const startChild = vi.fn();
    const runner = createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient,
      historyClient: {
        read: vi.fn(async () => ({
          schema: "h3.context.comfy_prompt_history_observation.v1" as const,
          promptId: "prompt.1",
          disposition: "succeeded" as const,
          outputNodeIds: Object.freeze(["save.video"]),
          outputIdentityFingerprint: fingerprint("e"),
          terminalFingerprint: fingerprint("f"),
        })),
      },
      reattachStore: store,
      startChild,
      resolveChild: vi.fn(),
    });

    await expect(runner.reattach()).resolves.toMatchObject({
      disposition: "artifact_authority_unavailable",
      activePromptId: "prompt.1",
    });
    expect(parentClient.send).not.toHaveBeenCalled();
    expect(coordinatorClient.send).not.toHaveBeenCalled();
    expect(startChild).not.toHaveBeenCalled();
  });

  it("refuses a decoded history observation whose prompt identity differs from the retained path", async () => {
    const store = createManagedSequenceReattachStore(
      memoryStorage(),
      () => 1_000,
    );
    store.replace({
      parentSequenceId,
      readAuthorityFingerprint: fingerprint("2"),
      expiresAtEpochMs: 10_000,
    });
    const projection = detachedProjection("artifact_verified");
    const coordinatorClient = { send: vi.fn() };
    const runner = createManagedSerialSequenceRunner({
      parentClient: {
        send: vi.fn(),
        read: vi.fn(async () => ({
          status: 200 as const,
          etag: projection.etag,
          projection,
        })),
      },
      coordinatorClient,
      historyClient: {
        read: vi.fn(async () => ({
          schema: "h3.context.comfy_prompt_history_observation.v1" as const,
          promptId: "prompt.foreign",
          disposition: "succeeded" as const,
          outputNodeIds: Object.freeze(["save.video"]),
          outputIdentityFingerprint: fingerprint("e"),
          terminalFingerprint: fingerprint("f"),
        })),
      },
      reattachStore: store,
      startChild: vi.fn(),
      resolveChild: vi.fn(),
    });

    await expect(runner.reattach()).resolves.toMatchObject({
      disposition: "artifact_authority_unavailable",
      activePromptId: "prompt.1",
    });
    expect(coordinatorClient.send).not.toHaveBeenCalled();
  });

  it("reconciles a retained artifact plus exact successful history and queues a successor only after explicit resume", async () => {
    const store = createManagedSequenceReattachStore(
      memoryStorage(),
      () => 1_000,
    );
    store.replace({
      parentSequenceId,
      readAuthorityFingerprint: fingerprint("2"),
      expiresAtEpochMs: 10_000,
    });
    const projection = detachedProjection("artifact_verified");
    const events: string[] = [];
    let revision = projection.revision;
    const parentClient = {
      read: vi.fn(async () => {
        events.push("parent:read");
        return { status: 200 as const, etag: projection.etag, projection };
      }),
      send: vi.fn(
        async (
          _requestId: string,
          action: string,
          payload: Record<string, unknown>,
        ) => {
          events.push(`parent:${action}`);
          expect(payload.expected_revision).toBe(revision);
          if (action === "record_terminal") {
            expect(payload).toMatchObject({
              segment_id: "segment.1",
              queue_prompt_id: "prompt.1",
              kind: "success",
              terminal_fingerprint: fingerprint("b"),
            });
            revision += 1;
            return mutationResult(revision, "paused_client_absent");
          }
          if (action === "resume_sequence") {
            revision += 1;
            return mutationResult(revision, "active");
          }
          if (action === "prepare_sequence_child") {
            expect(payload).toMatchObject({
              segment_id: "segment.2",
              predecessor_terminal_fingerprint: fingerprint("b"),
            });
            revision += 1;
            return mutationResult(
              revision,
              "active",
              execution("segment.2", revision, fingerprint("b")),
            );
          }
          if (action === "bind_prepared_child") {
            revision += 1;
            return mutationResult(revision, "active", null, {
              schema: "h3.context.managed_sequence_child_authority.v1",
              runHandle: `mc_${"2".repeat(40)}`,
              stateFingerprint: fingerprint("c"),
              jobId: "job.2",
              transactionId: "transaction.2",
            });
          }
          if (action === "record_submission") {
            revision += 2;
            return mutationResult(revision, "active");
          }
          throw new Error(`unexpected parent action ${action}`);
        },
      ),
    };
    const coordinatorClient = {
      send: vi.fn(
        async (
          _requestId: string,
          action: string,
          payload: Record<string, unknown>,
        ) => {
          events.push(`child:${action}`);
          const run = payload.run_handle as string;
          if (action === "record_terminal")
            return coordinatorResult(
              run,
              fingerprint("9"),
              "succeeded",
              {
                schema:
                  "h3.context.generation_coordinator.artifact_authority.v1",
                receiptFingerprint: fingerprint("a"),
                byteLength: 23,
              },
              fingerprint("b"),
            );
          if (action === "release_sequence")
            return coordinatorResult(run, fingerprint("9"), "released");
          if (action === "record_submission")
            return coordinatorResult(run, fingerprint("d"), "submitted");
          throw new Error(`unexpected coordinator action ${action}`);
        },
      ),
    };
    const historyClient = {
      read: vi.fn(async () => ({
        schema: "h3.context.comfy_prompt_history_observation.v1" as const,
        promptId: "prompt.1",
        disposition: "succeeded" as const,
        outputNodeIds: Object.freeze(["save.video"]),
        outputIdentityFingerprint: fingerprint("e"),
        terminalFingerprint: fingerprint("f"),
      })),
    };
    const workflowAuthority = {};
    let queueCalls = 0;
    const startChild = vi.fn(
      async (
        _inputs: AppModeInputs,
        options: AppModeStartOptions = {},
      ): Promise<AppModeStartResult> => {
        const prepared = preflight(fingerprint("0"));
        const queueAuthority = await options.prepareManaged!(prepared);
        queueAuthority.bindCanvasIdentity?.({
          workflowAuthority,
          ownedReference: {
            nodeIds: ["1"],
            linkIds: [],
            anchorNodeId: "1",
            authoredWidgetNodeIds: [],
          },
        });
        queueCalls += 1;
        queueAuthority.onQueueSubmitted();
        const result: AppModeStartResult = {
          queueResult: {
            prompt_id: "prompt.2",
            number: 2,
            nodeErrorClassTypes: Object.freeze([]),
            nodeErrorTypes: Object.freeze([]),
          },
          graphFingerprint: prepared.observation.graph_fingerprint,
          compiledPromptFingerprint:
            prepared.observation.compiled_prompt_fingerprint,
          ownedProjectionFingerprint: fingerprint("0"),
          ownedNodeIds: ["1"],
          ownedLinkIds: [],
          queuePromptId: "prompt.2",
          route: "existing",
        };
        await queueAuthority.onQueueAccepted(result);
        return result;
      },
    );
    const runner = createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient,
      historyClient,
      reattachStore: store,
      startChild,
      resolveChild: async () => ({
        inputs: {
          task_mode: "t2va",
          user_intent: "synthetic",
          duration_milliseconds: 8_000,
          frame_count: 192,
        },
        workflowAuthority,
        activeWorkflowFingerprint: fingerprint("8"),
        previousOwnedProjectionFingerprint: fingerprint("8"),
      }),
    });

    await expect(runner.reattach()).resolves.toMatchObject({
      disposition: "terminal_reconciled",
      parentState: "paused_client_absent",
      activePromptId: null,
    });
    expect(queueCalls).toBe(0);
    expect(events.indexOf("parent:read")).toBeLessThan(
      events.indexOf("child:record_terminal"),
    );
    expect(events.indexOf("child:release_sequence")).toBeLessThan(
      events.indexOf("parent:record_terminal"),
    );
    // A runner that never bound a child in this process has no label to offer: the reattach
    // pointer carries none, so resume after a reload cannot be revalidated from the runner.
    expect(runner.canvasLabel()).toBeNull();

    await runner.resume({
      activeWorkflowFingerprint: fingerprint("8"),
      ownedProjectionFingerprint: fingerprint("8"),
    });
    expect(queueCalls).toBe(1);
    expect(runner.canvasLabel()).toEqual({
      workflowAuthority,
      activeWorkflowFingerprint: fingerprint("8"),
    });
    expect(events.indexOf("parent:resume_sequence")).toBeLessThan(
      events.indexOf("parent:prepare_sequence_child"),
    );
  });

  it("does not restore successor authority when detach wins a pending explicit resume", async () => {
    const store = createManagedSequenceReattachStore(
      memoryStorage(),
      () => 1_000,
    );
    store.replace({
      parentSequenceId,
      readAuthorityFingerprint: fingerprint("2"),
      expiresAtEpochMs: 10_000,
    });
    const retained = detachedProjection();
    const projection: ManagedSequenceProjection = Object.freeze({
      ...retained,
      activeSegmentId: null,
      slots: Object.freeze([
        Object.freeze({
          ...retained.slots[0]!,
          state: "succeeded",
          terminalFingerprint: fingerprint("a"),
        }),
        retained.slots[1]!,
      ]),
    });
    const resumeResult = deferred<ManagedSequenceMutationResult>();
    let revision = projection.revision;
    const parentClient = {
      read: vi.fn(async () => ({
        status: 200 as const,
        etag: projection.etag,
        projection,
      })),
      send: vi.fn(
        async (
          _id: string,
          action: string,
        ): Promise<ManagedSequenceMutationResult> => {
          if (action === "resume_sequence") return resumeResult.promise;
          if (action === "detach_client") {
            revision += 1;
            return mutationResult(revision, "paused_client_absent");
          }
          throw new Error(`unexpected action ${action}`);
        },
      ),
    };
    const startChild = vi.fn();
    const runner = createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient: { send: vi.fn() },
      reattachStore: store,
      startChild,
      resolveChild: vi.fn(),
    });

    await expect(runner.reattach()).resolves.toMatchObject({
      disposition: "resumable",
    });
    const resuming = runner.resume({
      activeWorkflowFingerprint: fingerprint("8"),
      ownedProjectionFingerprint: fingerprint("9"),
    });
    await vi.waitFor(() =>
      expect(parentClient.send).toHaveBeenCalledWith(
        expect.any(String),
        "resume_sequence",
        expect.any(Object),
      ),
    );
    const detaching = runner.detach();
    revision += 1;
    resumeResult.resolve(mutationResult(revision, "active"));

    await expect(resuming).resolves.toBeUndefined();
    await expect(detaching).resolves.toBeUndefined();
    expect(
      parentClient.send.mock.calls.some(
        ([, action]) => action === "prepare_sequence_child",
      ),
    ).toBe(false);
    expect(startChild).not.toHaveBeenCalled();
    expect(runner.snapshot()).toMatchObject({
      attached: false,
      parentState: "paused_client_absent",
    });
  });

  it("reattaches a failed parent and retries the named child only after current canvas proof", async () => {
    const store = createManagedSequenceReattachStore(
      memoryStorage(),
      () => 1_000,
    );
    store.replace({
      parentSequenceId,
      readAuthorityFingerprint: fingerprint("2"),
      expiresAtEpochMs: 10_000,
    });
    const projection = detachedFailureProjection();
    let revision = projection.revision;
    const actions: string[] = [];
    const parentClient = {
      read: vi.fn(async () => ({
        status: 200 as const,
        etag: projection.etag,
        projection,
      })),
      send: vi.fn(
        async (
          _id: string,
          action: string,
          payload: Record<string, unknown>,
        ) => {
          actions.push(action);
          expect(payload.expected_revision).toBe(revision);
          if (action === "retry_segment") {
            expect(payload).toMatchObject({
              segment_id: "segment.1",
              active_workflow_fingerprint: fingerprint("8"),
              owned_projection_fingerprint: fingerprint("9"),
            });
            revision += 1;
            return mutationResult(revision, "active");
          }
          if (action === "prepare_sequence_child") {
            revision += 1;
            return mutationResult(
              revision,
              "active",
              execution("segment.1", revision),
            );
          }
          if (action === "bind_prepared_child") {
            revision += 1;
            return mutationResult(revision, "active", null, {
              schema: "h3.context.managed_sequence_child_authority.v1",
              runHandle: `mc_${"3".repeat(40)}`,
              stateFingerprint: fingerprint("a"),
              jobId: "job.retry.1",
              transactionId: "transaction.retry.1",
            });
          }
          if (action === "record_submission") {
            revision += 2;
            return mutationResult(revision, "active");
          }
          throw new Error(`unexpected parent action ${action}`);
        },
      ),
    };
    const coordinatorClient = {
      send: vi.fn(
        async (
          _id: string,
          action: string,
          payload: Record<string, unknown>,
        ) => {
          if (action === "record_submission")
            return coordinatorResult(
              payload.run_handle as string,
              fingerprint("b"),
              "submitted",
            );
          throw new Error(`unexpected coordinator action ${action}`);
        },
      ),
    };
    const workflowAuthority = {};
    let queueCalls = 0;
    const runner = createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient,
      reattachStore: store,
      startChild: async (_inputs, options = {}) => {
        const prepared = preflight(fingerprint("0"));
        const queueAuthority = await options.prepareManaged!(prepared);
        queueAuthority.bindCanvasIdentity?.({
          workflowAuthority,
          ownedReference: {
            nodeIds: ["1"],
            linkIds: [],
            anchorNodeId: "1",
            authoredWidgetNodeIds: [],
          },
        });
        queueCalls += 1;
        queueAuthority.onQueueSubmitted();
        const result: AppModeStartResult = {
          queueResult: {
            prompt_id: "prompt.retry.1",
            number: 3,
            nodeErrorClassTypes: Object.freeze([]),
            nodeErrorTypes: Object.freeze([]),
          },
          graphFingerprint: prepared.observation.graph_fingerprint,
          compiledPromptFingerprint:
            prepared.observation.compiled_prompt_fingerprint,
          ownedProjectionFingerprint: fingerprint("0"),
          ownedNodeIds: ["1"],
          ownedLinkIds: [],
          queuePromptId: "prompt.retry.1",
          route: "existing",
        };
        await queueAuthority.onQueueAccepted(result);
        return result;
      },
      resolveChild: async () => ({
        inputs: {
          task_mode: "t2va",
          user_intent: "synthetic",
          duration_milliseconds: 8_000,
          frame_count: 192,
        },
        workflowAuthority,
        activeWorkflowFingerprint: fingerprint("8"),
        previousOwnedProjectionFingerprint: fingerprint("9"),
      }),
    });

    await expect(runner.reattach()).resolves.toMatchObject({
      disposition: "current",
      parentState: "paused_failure",
    });
    expect(queueCalls).toBe(0);

    await runner.retry({
      segmentId: "segment.1",
      activeWorkflowFingerprint: fingerprint("8"),
      ownedProjectionFingerprint: fingerprint("9"),
    });
    expect(actions[0]).toBe("retry_segment");
    expect(queueCalls).toBe(1);
  });
});
