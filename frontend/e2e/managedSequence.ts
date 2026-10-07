import type {
  AppModeInputs,
  AppModeStartOptions,
  AppModeStartResult,
  ManagedAppModePreflight,
} from "../src/host/appMode";
import {
  createManagedSequenceReattachStore,
  createManagedSerialSequenceRunner,
  type ChildCoordinatorClient,
  type EligibleSegmentExecution,
  type ManagedParentClient,
  type ManagedSequenceMutationResult,
  type ManagedSerialSequenceStartIntent,
} from "../src/host/managedSequence";
import type { SequenceCoordinatorResult } from "../src/host/sequenceCoordinator";
import {
  ManagedSequenceClientError,
  decodeManagedSequenceProjection,
} from "../src/host/managedSequenceClient";

export type ManagedSequenceHarnessSnapshot = Readonly<{
  queueCalls: number;
  actions: readonly string[];
  runner: ReturnType<
    ReturnType<typeof createManagedSerialSequenceRunner>["snapshot"]
  >;
}>;

export type ManagedSequenceHarness = Readonly<{
  snapshot(): ManagedSequenceHarnessSnapshot;
  sendTerminalPair(promptId: string): Promise<void>;
  detach(): Promise<void>;
}>;

declare global {
  interface Window {
    h3ManagedSequenceHarness?: ManagedSequenceHarness;
  }
}

const fingerprint = (digit: string) => `sha256:${digit.repeat(64)}`;
const parentSequenceId = "managed.sequence.browser";

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

export function createManagedSequenceHarnessEngine(
  options: {
    /** The owned projection each child writes, as the page observes it on its own canvas. */
    ownedProjectionFingerprint?: string;
  } = {},
) {
  const ownedProjection =
    options.ownedProjectionFingerprint ?? fingerprint("7");
  // The opaque parent label every child is bound with; see resume_sequence below.
  const boundWorkflowLabel = fingerprint("e");
  let revision = -1;
  let activeSegment = "segment.1";
  let childNumber = 0;
  let childState = fingerprint("1");
  let detached = false;
  let queueCalls = 0;
  const actions: string[] = [];
  const artifactSeen = new Set<number>();
  const terminalSeen = new Set<number>();
  const completedSegments = new Set<string>();

  const mutation = (
    state: ManagedSequenceMutationResult["state"],
    execution: EligibleSegmentExecution | null = null,
    childAuthority: ManagedSequenceMutationResult["childAuthority"] = null,
  ): ManagedSequenceMutationResult =>
    Object.freeze({
      schema: "h3.context.managed_sequence_result.v1",
      parentSequenceId,
      authorizationFingerprint: fingerprint("1"),
      readAuthorityFingerprint: fingerprint("2"),
      state,
      revision,
      execution,
      childAuthority,
      contextWorkspaceHandle:
        execution === null ? null : `ws_${"c".repeat(40)}`,
      parentExpiresAtEpochMs: 87_400_000,
      replayed: false,
    });

  const parentClient: ManagedParentClient = {
    async send(_requestId, action, payload) {
      actions.push(`parent:${action}`);
      if (action === "authorize_sequence") {
        revision = 0;
        return mutation("authorized");
      }
      if (action === "start_sequence") {
        revision = 1;
        return mutation("active");
      }
      if (action === "prepare_sequence_child") {
        activeSegment = payload.segment_id as string;
        revision += 1;
        return mutation(
          "active",
          Object.freeze({
            parentSequenceId,
            parentAuthorizationFingerprint: fingerprint("1"),
            segmentId: activeSegment,
            slotRevision: revision,
            attemptEpoch: 1,
            materializationReceiptFingerprint: fingerprint("3"),
            predecessorTerminalFingerprint:
              payload.predecessor_terminal_fingerprint as string | null,
            requestId: `prepare.${activeSegment}`,
            jobCount: 1,
            fingerprint: fingerprint("4"),
          }),
        );
      }
      if (action === "bind_prepared_child") {
        childNumber += 1;
        revision += 1;
        childState = fingerprint(childNumber === 1 ? "5" : "6");
        return mutation("active", null, {
          schema: "h3.context.managed_sequence_child_authority.v1",
          runHandle: `mc_${String(childNumber).repeat(40)}`,
          stateFingerprint: childState,
          jobId: `job.${childNumber}`,
          transactionId: `transaction.${childNumber}`,
        });
      }
      if (action === "record_submission") {
        revision += 2;
        return mutation(detached ? "paused_client_absent" : "active");
      }
      if (action === "record_running" || action === "record_artifact") {
        revision += 1;
        return mutation(detached ? "paused_client_absent" : "active");
      }
      if (action === "record_terminal") {
        completedSegments.add(activeSegment);
        revision += 1;
        return mutation(
          activeSegment === "segment.2"
            ? "succeeded"
            : detached
              ? "paused_client_absent"
              : "active",
        );
      }
      if (action === "detach_client") {
        detached = true;
        revision += 1;
        return mutation("paused_client_absent");
      }
      if (action === "resume_sequence") {
        // IMPORTANT (B-M2522-RESUME-01): fence resume exactly as the backend does. An engine that
        // ignored this identity let a whole-graph hash pass here while the real parent refused it.
        if (
          payload.active_workflow_fingerprint !== boundWorkflowLabel ||
          payload.owned_projection_fingerprint !== ownedProjection
        )
          throw new ManagedSequenceClientError("canvas_drift", 409);
        detached = false;
        revision += 1;
        return mutation("active");
      }
      throw new Error(`unexpected parent action ${action}`);
    },
    async read() {
      actions.push("parent:read_projection");
      const projection = decodeManagedSequenceProjection({
        schema: "h3.context.managed_sequence_projection.v1",
        parent_sequence_id: parentSequenceId,
        authorization_fingerprint: fingerprint("1"),
        state:
          completedSegments.size === 2
            ? "succeeded"
            : detached
              ? "paused_client_absent"
              : "active",
        revision,
        etag: fingerprint("a"),
        lease: {
          schema: "h3.context.managed_sequence_lease.v1",
          created_at: "3ff0000000000000",
          idle_deadline: "4051800000000000",
          active_deadline: null,
          absolute_deadline: "4051800000000000",
          idle_expiry_handled: false,
        },
        active_segment_id: completedSegments.has(activeSegment)
          ? null
          : activeSegment,
        slots: ["segment.1", "segment.2"].map((segmentId, index) => ({
          schema: "h3.context.managed_sequence_slot.v1",
          segment_id: segmentId,
          ordinal: index + 1,
          state: completedSegments.has(segmentId)
            ? "succeeded"
            : segmentId === activeSegment
              ? "submitted"
              : "pending",
          attempt_epoch: 1,
          eligible_execution_fingerprint: fingerprint("4"),
          child_run_handle:
            segmentId === activeSegment
              ? `mc_${String(childNumber).repeat(40)}`
              : null,
          child_state_fingerprint:
            segmentId === activeSegment ? childState : null,
          graph_fingerprint: fingerprint("5"),
          compiled_prompt_fingerprint: fingerprint("6"),
          canvas_write_fingerprint: fingerprint("7"),
          queue_prompt_id:
            segmentId === activeSegment ? `prompt.${childNumber}` : null,
          artifact_receipt_fingerprint: completedSegments.has(segmentId)
            ? fingerprint("a")
            : null,
          terminal_fingerprint: completedSegments.has(segmentId)
            ? fingerprint("b")
            : null,
        })),
      });
      return { status: 200, etag: projection.etag, projection };
    },
  };

  const coordinatorResult = (
    disposition: SequenceCoordinatorResult["disposition"],
    artifact = false,
    terminal = false,
  ): SequenceCoordinatorResult =>
    ({
      schema: "h3.context.generation_coordinator.response.v1",
      runHandle: `mc_${String(childNumber).repeat(40)}`,
      disposition,
      artifactAuthority: artifact
        ? {
            schema: "h3.context.generation_coordinator.artifact_authority.v1",
            receiptFingerprint: fingerprint("a"),
            byteLength: 23,
          }
        : null,
      terminalFingerprint: terminal ? fingerprint("b") : null,
      sequence: { state_fingerprint: childState },
      production: {},
    }) as unknown as SequenceCoordinatorResult;

  const coordinatorClient: ChildCoordinatorClient = {
    async send(_requestId, action, payload) {
      actions.push(`child:${action}`);
      if (action === "record_submission") {
        childState = fingerprint("7");
        return coordinatorResult("submitted");
      }
      if (action === "record_running") {
        childState = fingerprint("8");
        return coordinatorResult("running");
      }
      if (action === "record_artifact") {
        artifactSeen.add(childNumber);
        childState = fingerprint("9");
        return coordinatorResult(
          terminalSeen.has(childNumber) ? "succeeded" : "artifact_verified",
          true,
          terminalSeen.has(childNumber),
        );
      }
      if (action === "record_terminal") {
        terminalSeen.add(childNumber);
        childState = fingerprint("c");
        return coordinatorResult(
          artifactSeen.has(childNumber) ? "succeeded" : "verification_pending",
          artifactSeen.has(childNumber),
          artifactSeen.has(childNumber),
        );
      }
      if (action === "release_sequence") {
        childState = fingerprint("d");
        return coordinatorResult(
          payload.intent === "detach_client" ? "detached" : "released",
          artifactSeen.has(childNumber),
          terminalSeen.has(childNumber),
        );
      }
      throw new Error(`unexpected child action ${action}`);
    },
  };

  const workflowAuthority = {};
  const reattachStore = createManagedSequenceReattachStore(
    memoryStorage(),
    () => 1_000,
  );
  const createRunner = () =>
    createManagedSerialSequenceRunner({
      parentClient,
      coordinatorClient,
      reattachStore,
      historyClient: {
        async read(promptId: string) {
          actions.push(`history:${promptId}`);
          return {
            schema: "h3.context.comfy_prompt_history_observation.v1",
            promptId,
            disposition: "not_yet_observed",
            terminalFingerprint: null,
            outputNodeIds: [],
            outputIdentityFingerprint: null,
          };
        },
      },
      async startChild(
        _inputs: AppModeInputs,
        options: AppModeStartOptions = {},
      ): Promise<AppModeStartResult> {
        const prepared: ManagedAppModePreflight = {
          bootstrap: { output: {}, workflow: {} },
          observation: {
            schema: "h3.context.prepared_graph_observation.v4",
            route: "existing",
            graph_fingerprint: fingerprint("5"),
            compiled_prompt_fingerprint: fingerprint("6"),
            owned_projection_fingerprint: ownedProjection,
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
        actions.push(`queue:${activeSegment}`);
        authority.onQueueSubmitted();
        const result: AppModeStartResult = {
          queueResult: {
            prompt_id: `prompt.${childNumber}`,
            number: childNumber,
            nodeErrorClassTypes: Object.freeze([]),
            nodeErrorTypes: Object.freeze([]),
          },
          graphFingerprint: fingerprint("5"),
          compiledPromptFingerprint: fingerprint("6"),
          ownedProjectionFingerprint: ownedProjection,
          ownedNodeIds: ["1"],
          ownedLinkIds: [],
          queuePromptId: `prompt.${childNumber}`,
          route: "existing",
        };
        await authority.onQueueAccepted(result);
        return result;
      },
      async resolveChild(_execution, _context, index) {
        return {
          inputs: {
            task_mode: "t2va",
            user_intent: "synthetic",
            duration_milliseconds: 8_000,
            frame_count: 192,
          },
          options: { useExisting: true },
          workflowAuthority,
          activeWorkflowFingerprint: boundWorkflowLabel,
          previousOwnedProjectionFingerprint:
            index === 0 ? fingerprint("0") : ownedProjection,
        };
      },
    });

  let runner = createRunner();

  const start: ManagedSerialSequenceStartIntent = Object.freeze({
    authorization: Object.freeze({
      workspace_handle: `pw_${"w".repeat(40)}`,
      expected_workspace_revision: 1,
      expected_workspace_fingerprint: fingerprint("a"),
      expected_plan_fingerprint: fingerprint("b"),
      generation_plan_fingerprint: fingerprint("c"),
      compiler_fingerprint: fingerprint("d"),
      host_capability_fingerprint: fingerprint("e"),
      explicit_intent: "generate_approved_sequence",
    }),
    qualificationFingerprint: fingerprint("f"),
    segmentIds: Object.freeze(["segment.1", "segment.2"]),
  });

  return Object.freeze({
    startIntent: start,
    workflowAuthority,
    reattachStore,
    parentClient,
    runner: () => runner,
    recreateRunner: () => {
      runner = createRunner();
    },
    snapshot: () =>
      Object.freeze({
        queueCalls,
        actions: Object.freeze([...actions]),
        runner: runner.snapshot(),
      }),
    async sendTerminalPair(promptId: string) {
      await runner.recordTerminal({ promptId, kind: "success" });
      await runner.recordArtifact({
        promptId,
        outputNodeId: "save.video",
        locator: {
          filename: "synthetic.mp4",
          subfolder: "",
          type: "output",
        },
      });
    },
    detach: () => runner.detach(),
  });
}

export function mountManagedSequenceHarness(root: HTMLElement): void {
  const engine = createManagedSequenceHarnessEngine();
  root.innerHTML = `
    <main aria-label="Managed sequence harness">
      <button id="start-managed" type="button">Generate approved sequence</button>
      <output id="managed-status">idle</output>
    </main>`;
  root.querySelector("#start-managed")?.addEventListener("click", () => {
    void engine
      .runner()
      .start(engine.startIntent)
      .then(
        () => {
          root.querySelector("#managed-status")!.textContent = "active";
        },
        () => {
          root.querySelector("#managed-status")!.textContent = "failed";
        },
      );
  });

  window.h3ManagedSequenceHarness = engine;
}
