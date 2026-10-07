import { describe, expect, it, vi } from "vitest";

import type {
  AppModeInputs,
  AppModeStartOptions,
  AppModeStartResult,
  ManagedAppModePreflight,
} from "../src/host/appMode";
import { createManagedSequenceReattachStore } from "../src/host/managedSequence";
import type {
  EligibleSegmentExecution,
  ManagedSequenceMutationResult,
} from "../src/host/managedSequence";
import type { SequenceCoordinatorResult } from "../src/host/sequenceCoordinator";
import { createAppModeMachine } from "../src/lifecycle/appModeMachine";
import { createAppModeSession } from "../src/lifecycle/appModeSession";
import { createAppModeCorrelation } from "../src/lifecycle/appModeCorrelation";
import { createAppModeInterpreter } from "../src/lifecycle/interpreter";
import {
  createShellSession,
  type ShellActions,
  type ShellDeps,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";

const fingerprint = (digit: string) => `sha256:${digit.repeat(64)}`;
const parentSequenceId = "managed.sequence.integration";

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
  state: ManagedSequenceMutationResult["state"],
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

function eligible(revision: number): EligibleSegmentExecution {
  return Object.freeze({
    parentSequenceId,
    parentAuthorizationFingerprint: fingerprint("1"),
    segmentId: "segment.1",
    slotRevision: revision,
    attemptEpoch: 1,
    materializationReceiptFingerprint: fingerprint("3"),
    predecessorTerminalFingerprint: null,
    requestId: "prepare.segment.1",
    jobCount: 1,
    fingerprint: fingerprint("4"),
  });
}

function coordinatorResult(
  disposition: SequenceCoordinatorResult["disposition"],
  stateFingerprint: string,
  terminalFingerprint: string | null = null,
): SequenceCoordinatorResult {
  return {
    schema: "h3.context.generation_coordinator.response.v1",
    runHandle: `mc_${"1".repeat(40)}`,
    disposition,
    artifactAuthority: null,
    terminalFingerprint,
    sequence: { state_fingerprint: stateFingerprint },
    production: {},
  } as unknown as SequenceCoordinatorResult;
}

function preflight(): ManagedAppModePreflight {
  return {
    bootstrap: { output: {}, workflow: {} },
    observation: {
      schema: "h3.context.prepared_graph_observation.v4",
      route: "existing",
      graph_fingerprint: fingerprint("5"),
      compiled_prompt_fingerprint: fingerprint("6"),
      owned_projection_fingerprint: fingerprint("7"),
      owned_node_ids: ["1"],
      owned_link_ids: [],
      model_fingerprint: fingerprint("8"),
      runtime_fingerprint: fingerprint("9"),
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

describe("App Mode managed serial integration", () => {
  it.each(["error", "completed child", "superseded binding"] as const)(
    "routes %s through the real child lifecycle without duplicating queue ownership",
    async (outcome) => {
      const events: string[] = [];
      let revision = -1;
      let childState = fingerprint("a");
      const parentClient = {
        send: vi.fn(
          async (
            _requestId: string,
            action: string,
          ): Promise<ManagedSequenceMutationResult> => {
            events.push(`parent:${action}`);
            if (action === "authorize_sequence") {
              revision = 0;
              return mutationResult(revision, "authorized");
            }
            if (action === "start_sequence") {
              revision = 1;
              return mutationResult(revision, "active");
            }
            if (action === "prepare_sequence_child") {
              revision += 1;
              return mutationResult(revision, "active", eligible(revision));
            }
            if (action === "bind_prepared_child") {
              revision += 1;
              return mutationResult(revision, "active", null, {
                schema: "h3.context.managed_sequence_child_authority.v1",
                runHandle: `mc_${"1".repeat(40)}`,
                stateFingerprint: childState,
                jobId: "job.1",
                transactionId: "transaction.1",
              });
            }
            if (action === "record_submission") {
              revision += 2;
              return mutationResult(revision, "active");
            }
            if (action === "record_running") {
              revision += 1;
              return mutationResult(revision, "active");
            }
            if (action === "record_terminal" || action === "fail_bound_child") {
              revision += 1;
              return mutationResult(revision, "paused_failure");
            }
            if (action === "detach_client") {
              revision += 1;
              return mutationResult(revision, "paused_client_absent");
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
          ): Promise<SequenceCoordinatorResult> => {
            events.push(`child:${action}`);
            if (action === "record_submission") {
              childState = fingerprint("b");
              return coordinatorResult("submitted", childState);
            }
            if (action === "record_running") {
              childState = fingerprint("c");
              return coordinatorResult("running", childState);
            }
            if (action === "record_terminal") {
              childState = fingerprint("d");
              return coordinatorResult("failed", childState, fingerprint("e"));
            }
            if (action === "release_sequence")
              return coordinatorResult(
                "released",
                childState,
                fingerprint("e"),
              );
            throw new Error(`unexpected child action ${action}`);
          },
        ),
      };

      const session = createShellSession();
      session.appModeAdmissions.set("t2va", { status: "admitted" });
      const lifecycle = createAppModeInterpreter({
        machine: createAppModeMachine(),
        onInvalidate: () => {
          void shellActions.detachManagedSerialSequence();
        },
      });
      const workflowAuthority = {};
      const shellActions = {
        serializeGraphForDiagnostics: vi.fn(() => ({})),
        releaseProjectionQuarantineIfGraphChanged: vi.fn(),
        clearDeferredAppModeProjections: vi.fn(),
        clearDeferredAppModeTerminals: vi.fn(),
        clearDeferredManagedArtifacts: vi.fn(),
        clearActiveAppModeExecution: vi.fn(),
        currentProjection: vi.fn(() => undefined),
        renderCurrent: vi.fn(),
        recordManagedStage: vi.fn(),
        recordSurroundingsEvidence: vi.fn(),
      } as unknown as ShellActions;
      const queueStart = vi.fn(
        async (
          _inputs: AppModeInputs,
          options: AppModeStartOptions = {},
        ): Promise<AppModeStartResult> => {
          const prepared = preflight();
          const authority = await options.prepareManaged!(prepared);
          if (outcome === "superseded binding") lifecycle.invalidate();
          authority.bindCanvasIdentity?.({
            workflowAuthority,
            ownedReference: {
              nodeIds: ["1"],
              linkIds: [],
              anchorNodeId: "1",
              authoredWidgetNodeIds: [],
            },
          });
          events.push("queue");
          authority.onQueueSubmitted();
          // The host may announce running before queuePrompt's Promise returns its prompt ID.
          shellActions.observeManagedSerialRunning("prompt.1");
          shellActions.observeManagedSerialRunning("prompt.foreign");
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
            ownedProjectionFingerprint:
              prepared.observation.owned_projection_fingerprint,
            ownedNodeIds: ["1"],
            ownedLinkIds: [],
            queuePromptId: "prompt.1",
            route: "existing",
          };
          await authority.onQueueAccepted(result);
          return result;
        },
      );
      const deps = {
        managedSequenceClient: parentClient,
        managedSequenceReattachStore: createManagedSequenceReattachStore(
          memoryStorage(),
          () => 1_000,
        ),
        comfyPromptHistoryClient: { read: vi.fn() },
        sequenceCoordinator: coordinatorClient,
        appModeLifecycle: lifecycle,
        appModeController: {
          capability: () => ({ status: "ready" as const }),
          admission: vi.fn(),
          start: queueStart,
        },
      } as unknown as ShellDeps;
      const runtime = Object.freeze({
        session,
        deps,
        actions: shellActions,
      }) satisfies ShellRuntime;
      Object.assign(shellActions, createAppModeSession(runtime));
      shellActions.managedCanvasIdentityFromPreparation =
        createAppModeCorrelation(runtime).managedCanvasIdentityFromPreparation;

      const start = shellActions.startManagedSerialSequence(
        {
          authorization: {
            workspace_handle: `pw_${"w".repeat(40)}`,
            expected_workspace_revision: 1,
            expected_workspace_fingerprint: fingerprint("1"),
            expected_plan_fingerprint: fingerprint("2"),
            generation_plan_fingerprint: fingerprint("3"),
            compiler_fingerprint: fingerprint("4"),
            host_capability_fingerprint: fingerprint("5"),
            explicit_intent: "generate_approved_sequence",
          },
          qualificationFingerprint: fingerprint("6"),
          segmentIds: ["segment.1"],
        },
        {
          resolveChild: async () => ({
            inputs: {
              task_mode: "t2va",
              user_intent: "synthetic",
              duration_milliseconds: 8_000,
              frame_count: 192,
            },
            options: { useExisting: true },
            workflowAuthority,
            activeWorkflowFingerprint: fingerprint("7"),
            previousOwnedProjectionFingerprint: fingerprint("8"),
          }),
        },
      );
      if (outcome === "superseded binding") {
        await expect(start).rejects.toMatchObject({
          code: "ambiguous_host_ownership",
        });
        expect(events).not.toContain("queue");
        expect(session.pendingAppMode?.managedIdentity).toBeUndefined();
        expect(shellActions.managedSerialSequenceSnapshot().attached).toBe(
          false,
        );
        return;
      }
      await start;
      await shellActions.settleManagedSerialSequence();

      expect(queueStart).toHaveBeenCalledOnce();
      expect(events.filter((event) => event === "queue")).toHaveLength(1);
      expect(
        parentClient.send.mock.calls.filter(
          ([, action]) => action === "record_running",
        ),
      ).toHaveLength(1);

      if (outcome === "completed child") {
        expect(session.pendingAppMode?.managedIdentity).toEqual({
          workflowAuthority,
          ownedReference: {
            nodeIds: ["1"],
            linkIds: [],
            anchorNodeId: "1",
            authoredWidgetNodeIds: [],
          },
          ownedProjectionFingerprint:
            preflight().observation.owned_projection_fingerprint,
        });
        const run = lifecycle.getRunSequence();
        const signal = lifecycle.signalFor(run);
        shellActions.completeAppModeLifecycle(run);
        await shellActions.settleManagedSerialSequence();
        expect(lifecycle.getSnapshot().value).toBe("terminal.done");
        expect(lifecycle.isCurrent(run)).toBe(false);
        expect(signal?.aborted).toBe(true);
        expect(shellActions.managedSerialSequenceSnapshot()).toMatchObject({
          parentState: "active",
          attached: true,
          failure: null,
        });
        expect(events).not.toContain("parent:detach_client");
        return;
      }
      shellActions.observeManagedSerialTerminal({
        promptId: "prompt.1",
        kind: "error",
      });
      await shellActions.settleManagedSerialSequence();

      expect(shellActions.managedSerialSequenceSnapshot()).toMatchObject({
        parentState: "paused_failure",
        activeSegmentId: null,
        failure: null,
      });
      expect(events.indexOf("child:release_sequence")).toBeLessThan(
        events.indexOf("parent:record_terminal"),
      );
    },
  );
});
