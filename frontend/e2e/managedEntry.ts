import {
  fingerprint,
  graphFingerprint,
  type AppModeCompiledPrompt,
} from "../src/host/appMode";
import { H3_NODE_TYPES } from "../src/host/graphAdapter";
import {
  dispatchExecutionTerminal,
  dispatchHostReconnected,
  dispatchHostReconnecting,
  dispatchHostSocketDropped,
  dispatchProjection,
  dispatchSaveVideoArtifact,
  durationResolutionRequests,
  lifecycleEvents,
  queuedPrompts,
  registeredExtension,
  registeredTab,
  setFetchApiHandler,
  setGraph,
  setGraphToPromptHandler,
  setQueuePromptIds,
  setQueuePromptFailure,
  setQueuePromptWrapperMode,
  sideEffectCounts,
  type QueuePromptWrapperMode,
} from "../tests/fixtures/entryHostModules";
import { canonicalDurationResolutions } from "../src/contracts/generatedDurationResolution";
import {
  ASSET_RELOCATED,
  MISSING_ASSET,
  generationProfileWire,
} from "../tests/support/generationProfileFixture";
import { existingManagedHostGraphFixture } from "../tests/support/managedHostGraphFixture";
import { rejectedQueueError } from "../tests/support/queuePromptTestDouble";
import { syntheticTemplate } from "../tests/support/templateFixture";
import {
  managedCoordinatorResponse,
  managedMemberCoordinatorResponse,
  managedLifecycleWires,
  type ManagedDurationWire,
  type ManagedLifecycleState,
} from "../tests/support/managedLifecycleWire";
import { syntheticPromptUuid } from "../tests/support/queuePromptTestDouble";
import { OFFICIAL_LENGTH_EXPRESSION } from "../src/host/templateMaterialization";
import { productionAccumulatedProjectFingerprint } from "../src/contracts/productionAccumulationCodec";
import { publicCompositionFingerprint } from "../src/contracts/compositionCodec";
import { projectionWire as authoringProjectionWire } from "../tests/support/authoringFixture";
import {
  FIXTURE_WORKSPACE_HANDLE,
  historyWire,
  SMOKE_SHAPE,
} from "../tests/support/nleWorkspaceFixture";
import { importResponseWire } from "../tests/support/productionAuthoringImportFixture";

type JsonRecord = Record<string, unknown>;
type ManagedWires = ReturnType<typeof managedLifecycleWires>;
const MANAGED_PROMPT_ID = syntheticPromptUuid(201);
const MANAGED_PROMPT_IDS = [
  MANAGED_PROMPT_ID,
  syntheticPromptUuid(202),
  syntheticPromptUuid(203),
] as const;
const ACCUMULATED_SEGMENTS_KEY = "h3-m25-36-managed-project-segments";

export type ManagedEntryHarnessSnapshot = Readonly<{
  queueCount: number;
  queueNodeTypes: readonly (readonly string[])[];
  queueFrameCounts: readonly number[];
  durationResolutionRequests: readonly number[];
  coordinatorExpectedFrames: readonly number[];
  coordinatorActions: readonly string[];
  productionActions: readonly string[];
  authoringActions: readonly string[];
  importCount: number;
  lifecycleEvents: readonly string[];
  managedRequestIds: readonly string[];
  requestLedgerIds: readonly string[];
  replayCollisionCount: number;
  queuePromptWrapperMode: QueuePromptWrapperMode;
  projectWorkspaceHandle: string | null;
  projectSegmentCount: number;
}>;

/**
 * The run state the coordinator has on record when a reconnect reads it back.
 *
 * `succeeded` and `unknown` override the ordinary stateful projection so reconnect journeys can
 * exercise already-terminal and lost-authority outcomes explicitly.
 */
export type ManagedReconcileRecord = "running" | "succeeded" | "unknown";

export type ManagedEntryHarness = Readonly<{
  snapshot(): ManagedEntryHarnessSnapshot;
  dispatchBootstrap(): void;
  dispatchTerminal(): void;
  dispatchFailure(): void;
  dispatchInterruption(): void;
  dispatchArtifact(): void;
  setReconcileRecord(record: ManagedReconcileRecord): void;
  dropHostSocket(): void;
  reconnectingHostSocket(): void;
  restoreHostSocket(): void;
  destroySidebar(): void;
  renderSidebar(): void;
}>;

declare global {
  interface Window {
    h3ManagedEntryHarness?: ManagedEntryHarness;
  }
}

function object(value: unknown): JsonRecord | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : undefined;
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function outputOf(compiled: AppModeCompiledPrompt): JsonRecord {
  return compiled.output;
}

function nodeIdFor(compiled: AppModeCompiledPrompt, classType: string): string {
  const matches = Object.entries(outputOf(compiled)).filter(
    ([, node]) => object(node)?.class_type === classType,
  );
  if (matches.length !== 1)
    throw new Error(`managed entry fixture needs exactly one ${classType}`);
  return matches[0]![0];
}

function queueNodeTypes(): readonly (readonly string[])[] {
  return queuedPrompts().map((queued) => {
    const output = object(queued)?.output;
    return Object.values(object(output) ?? {})
      .map((node) => object(node)?.class_type)
      .filter((value): value is string => typeof value === "string")
      .sort();
  });
}

function queueFrameCounts(): readonly number[] {
  return queuedPrompts().map((queued) => {
    const output = object(object(queued)?.output);
    const anchor = Object.values(output ?? {}).find((node) => {
      const classType = object(node)?.class_type;
      return (
        classType === "MiniMaxH3ImageToVideo" ||
        classType === "MiniMaxH3ReferenceToVideo"
      );
    });
    const length = object(object(anchor)?.inputs)?.length;
    if (typeof length === "number") return length;
    if (!Array.isArray(length) || length.length !== 2)
      throw new Error("queued H3 prompt has no length authority");
    const math = object(output?.[String(length[0])]);
    const mathInputs = object(math?.inputs);
    const durationLink = mathInputs?.["values.a"];
    if (
      math?.class_type !== "ComfyMathExpression" ||
      mathInputs?.expression !== OFFICIAL_LENGTH_EXPRESSION ||
      !Array.isArray(durationLink) ||
      durationLink.length !== 2
    )
      throw new Error("queued H3 prompt has a foreign length authority");
    const durationSource = object(output?.[String(durationLink[0])]);
    const seconds = object(durationSource?.inputs)?.value;
    const canonical = canonicalDurationResolutions.find(
      (candidate) => candidate.requested_seconds === seconds,
    );
    if (
      durationSource?.class_type !== "PrimitiveFloat" ||
      canonical === undefined
    )
      throw new Error("queued H3 prompt has no canonical duration source");
    return canonical.frame_count;
  });
}

const REQUEST_LEDGER_KEY = "h3-m23-15-managed-request-ledger";

function requestLedgerIds(): string[] {
  const encoded = window.localStorage.getItem(REQUEST_LEDGER_KEY);
  if (encoded === null) return [];
  const parsed: unknown = JSON.parse(encoded);
  if (
    !Array.isArray(parsed) ||
    parsed.some((value) => typeof value !== "string")
  )
    throw new Error("managed entry request ledger is invalid");
  return parsed;
}

function rememberRequestId(requestId: string): boolean {
  const ledger = requestLedgerIds();
  if (ledger.includes(requestId)) return false;
  ledger.push(requestId);
  window.localStorage.setItem(REQUEST_LEDGER_KEY, JSON.stringify(ledger));
  return true;
}

function accumulatedSegmentCount(): number {
  const value = Number(window.sessionStorage.getItem(ACCUMULATED_SEGMENTS_KEY));
  return Number.isSafeInteger(value) && value >= 0 && value <= 64 ? value : 0;
}

function rememberAccumulatedSegmentCount(value: number): void {
  window.sessionStorage.setItem(ACCUMULATED_SEGMENTS_KEY, String(value));
}

export function accumulatedLifecycleWires(
  graphFingerprintValue: string,
  compiledPromptFingerprint: string,
  productShellNodeId: string,
  queuePromptId: string,
  duration: ManagedDurationWire,
  ordinal: number,
  attemptOrdinal = ordinal,
): ManagedWires {
  const next = structuredClone(
    managedLifecycleWires(
      graphFingerprintValue,
      compiledPromptFingerprint,
      productShellNodeId,
      queuePromptId,
      duration,
    ),
  ) as unknown as ManagedWires;
  const segmentId = `segment_${ordinal}`;
  const stateNames: readonly ManagedLifecycleState[] = [
    "planned",
    "submitted",
    "running",
    "succeeded",
  ];
  for (const state of stateNames) {
    const sequence = next[state];
    sequence.sequence_id = `sequence.${ordinal}.attempt.${attemptOrdinal}`;
    sequence.sequence_fingerprint = `sha256:${Math.min(attemptOrdinal + 8, 15)
      .toString(16)
      .repeat(64)}`;
    sequence.workspace_revision = ordinal;
    sequence.workspace_fingerprint = `sha256:${String(
      Math.min(ordinal + 1, 15).toString(16),
    ).repeat(64)}`;
    const progress = sequence.progress[0]! as unknown as JsonRecord;
    progress.job_id = `job.${ordinal}.attempt.${attemptOrdinal}`;
    progress.segment_id = segmentId;
    // A member response carries its private one-job sequence, not the accumulating project order.
    progress.ordinal = 1;
    progress.attempt = attemptOrdinal;
    progress.transaction_id = `generation.command.${ordinal}.attempt.${attemptOrdinal}`;
    const command = sequence.eligible_commands[0] as
      (typeof sequence.eligible_commands)[number] | undefined;
    if (command !== undefined) {
      command.job_id = `job.${ordinal}.attempt.${attemptOrdinal}`;
      command.segment_id = segmentId;
      command.ordinal = 1;
      command.attempt = attemptOrdinal;
      command.transaction_id = `generation.command.${ordinal}.attempt.${attemptOrdinal}`;
    }

    const production = next.production[state] as JsonRecord;
    const current = (production.segments as JsonRecord[])[0]!;
    current.segment_id = segmentId;
    current.ordinal = ordinal;
    const prior = Array.from({ length: ordinal - 1 }, (_, index) => ({
      ...structuredClone(current),
      segment_id: `segment_${index + 1}`,
      ordinal: index + 1,
      closure_state: "clean",
      job_state: "succeeded",
      artifact_state: "complete",
    }));
    production.workspace_revision = ordinal;
    production.workspace_fingerprint = sequence.workspace_fingerprint;
    production.segments = [...prior, current];
    production.selected_segment_ids = [segmentId];
    production.run = {
      state:
        state === "planned"
          ? "ready"
          : state === "succeeded"
            ? "succeeded"
            : "running",
      completed: ordinal - 1 + (state === "succeeded" ? 1 : 0),
      total: ordinal,
    };
    const currentOutputs = production.outputs as JsonRecord[];
    production.outputs = [
      ...Array.from({ length: ordinal - 1 }, (_, index) => ({
        output_handle: `out_${String(index + 1).repeat(40)}`,
        ordinal: index + 1,
        state: "ready",
        segment_id: `segment_${index + 1}`,
        preview: true,
      })),
      ...currentOutputs.map((output) => ({
        ...output,
        output_handle: `out_${String(ordinal).repeat(40)}`,
        ordinal,
        segment_id: segmentId,
      })),
    ];
    if (
      (production.outputs as JsonRecord[]).length > 0 &&
      !(production.allowed_actions as string[]).includes("preview_output")
    )
      (production.allowed_actions as string[]).push("preview_output");
    if (
      (production.outputs as JsonRecord[]).length > 0 &&
      !(production.allowed_actions as string[]).includes(
        "import_production_outputs_to_authoring",
      )
    )
      (production.allowed_actions as string[]).push(
        "import_production_outputs_to_authoring",
      );
    const generation = object(production.generation_sequence);
    if (generation !== undefined) {
      generation.sequence_id = sequence.sequence_id;
      generation.sequence_fingerprint = sequence.sequence_fingerprint;
      generation.workspace_revision = ordinal;
      generation.workspace_fingerprint = sequence.workspace_fingerprint;
    }
  }
  return next;
}

export async function mountManagedEntryHarness(
  root: HTMLElement,
  options: Readonly<{
    artifactFailure?: boolean;
    assetRelocated?: boolean;
    missingAsset?: boolean;
    queuePromptWrapperMode?: QueuePromptWrapperMode;
    durationSeconds?: number;
    initialGraph?: "existing" | "empty" | "dirty";
    queueRejection?: boolean;
  }> = {},
): Promise<void> {
  const duration = canonicalDurationResolutions.find(
    (candidate) =>
      candidate.requested_seconds === (options.durationSeconds ?? 8),
  );
  if (duration === undefined)
    throw new Error("managed entry fixture needs a canonical duration");
  const userIntent =
    duration.requested_seconds === 8
      ? "A synthetic eight-second managed H3 browser journey."
      : `A synthetic ${duration.requested_seconds}-second managed H3 browser journey.`;
  const inputs = Object.freeze({
    task_mode: "t2va" as const,
    user_intent: userIntent,
    duration_milliseconds: duration.requested_milliseconds,
    frame_count: duration.frame_count,
  });
  const graph = existingManagedHostGraphFixture(inputs);
  const compiled = structuredClone(graph.compiled);
  const productShellNodeId = nodeIdFor(compiled, H3_NODE_TYPES.productShell);
  const saveVideoNodeId = nodeIdFor(compiled, "SaveVideo");
  const initialWorkflow =
    options.initialGraph !== undefined && options.initialGraph !== "existing"
      ? {
          last_node_id: 0,
          last_link_id: 0,
          nodes:
            options.initialGraph === "dirty"
              ? [{ id: 999, type: "PrimitiveNode", widgets_values: [1] }]
              : [],
          links: [],
          groups: [],
          config: {},
          extra: {},
          version: 0.4,
        }
      : graph.workflow;
  const initialGraphFingerprint = graphFingerprint(initialWorkflow);
  let completedSegments = accumulatedSegmentCount();
  let activeOrdinal = Math.max(1, completedSegments + 1);
  let generationAttemptOrdinal = 0;
  let activePromptId = MANAGED_PROMPT_ID;
  let wires: ManagedWires = accumulatedLifecycleWires(
    initialGraphFingerprint,
    fingerprint(compiled),
    productShellNodeId,
    activePromptId,
    {
      duration_milliseconds: duration.effective_milliseconds,
      delivered_milliseconds: duration.effective_milliseconds,
      frame_count: duration.frame_count,
      snapped: false,
    },
    activeOrdinal,
  );
  let projectProjection: JsonRecord | undefined =
    completedSegments === 0
      ? undefined
      : (accumulatedLifecycleWires(
          initialGraphFingerprint,
          fingerprint(compiled),
          productShellNodeId,
          activePromptId,
          {
            duration_milliseconds: duration.effective_milliseconds,
            delivered_milliseconds: duration.effective_milliseconds,
            frame_count: duration.frame_count,
            snapped: false,
          },
          completedSegments,
        ).production.succeeded as JsonRecord);
  let projectAttemptStatus = "succeeded";
  const accumulatedProject = (): JsonRecord => {
    const identity = (projectProjection ??
      wires.production.planned) as JsonRecord;
    const committed = projectAttemptStatus === "succeeded";
    const material = {
      schema: "h3.context.production_accumulated_project.v1",
      workspace_handle: identity.workspace_handle,
      workspace_id: identity.workspace_id,
      project_revision: Math.max(1, activeOrdinal + 1),
      workspace: projectProjection ?? null,
      attempts:
        projectAttemptStatus === "succeeded" && projectProjection === undefined
          ? []
          : [
              {
                candidate_id: `segment_${activeOrdinal}`,
                attempt_id: `attempt_${activeOrdinal}`,
                member_segment_id: `segment_${activeOrdinal}`,
                status: projectAttemptStatus,
                recovery:
                  projectAttemptStatus === "output_verification_failed"
                    ? "verify_output"
                    : projectAttemptStatus === "failed" ||
                        projectAttemptStatus === "cancelled"
                      ? "retry"
                      : null,
                committed,
              },
            ],
      capabilities: ["read", "admit_generation", "release_generation"],
    };
    return {
      ...material,
      project_fingerprint: productionAccumulatedProjectFingerprint(material),
    };
  };
  let memberRun = false;
  const coordinatorActions: string[] = [];
  const productionActions: string[] = [];
  const authoringActions: string[] = [];
  let importCount = 0;
  const authoringWorkspaceHandle = FIXTURE_WORKSPACE_HANDLE;
  const preparedAuthoringProjection = authoringProjectionWire({
    workspace_handle: authoringWorkspaceHandle,
    context_source_id: wires.workspaceId,
  });
  const preparedHistory = historyWire(SMOKE_SHAPE);
  // M25-40: an accepted import is focused only after the editor's own history read shows its
  // receipt assets, so the double answers later history reads with the assets it imported.
  const importedAssetIds: string[] = [];
  const currentHistory = (): JsonRecord => {
    if (importedAssetIds.length === 0) return preparedHistory;
    const history = structuredClone(preparedHistory) as JsonRecord;
    const snapshot = object(history.snapshot)!;
    const assets = snapshot.assets as JsonRecord[];
    snapshot.assets = [
      ...assets,
      ...importedAssetIds.map((assetId) => ({
        ...structuredClone(assets[0]!),
        asset_id: assetId,
      })),
    ];
    snapshot.public_fingerprint = publicCompositionFingerprint(snapshot);
    return history;
  };
  const managedRequestIds: string[] = [];
  const coordinatorExpectedFrames: number[] = [];
  let replayCollisionCount = 0;
  let reconcileRecord: ManagedReconcileRecord = "running";
  let successTerminalPendingArtifact = false;

  setGraph(structuredClone(initialWorkflow) as JsonRecord);
  setGraphToPromptHandler(() => structuredClone(compiled));
  setQueuePromptIds([...MANAGED_PROMPT_IDS]);
  setQueuePromptWrapperMode(options.queuePromptWrapperMode ?? "none");
  if (options.queueRejection)
    setQueuePromptFailure(rejectedQueueError("synthetic model"));
  if (options.initialGraph === "empty" || options.initialGraph === "dirty") {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (input, init) => {
      const url = String(input);
      if (url.endsWith("/templates/video_minimax_h3_t2v.json"))
        return new Response(
          JSON.stringify(syntheticTemplate("video_minimax_h3_t2v")),
          { status: 200 },
        );
      return originalFetch(input, init);
    };
  }
  setFetchApiHandler(async (path, init) => {
    if (path === "/h3-context/v1/generation/profile")
      return jsonResponse(
        generationProfileWire(
          options.missingAsset === true
            ? { text: MISSING_ASSET }
            : options.assetRelocated === true
              ? { text: ASSET_RELOCATED }
              : undefined,
        ),
      );

    const body = object(JSON.parse(String(init.body ?? "{}"))) ?? {};
    const action = typeof body.action === "string" ? body.action : "";
    const payload = object(body.payload) ?? {};
    if (
      typeof body.request_id === "string" &&
      body.request_id.startsWith("managed.")
    ) {
      managedRequestIds.push(body.request_id);
      if (!rememberRequestId(body.request_id)) {
        replayCollisionCount += 1;
        return jsonResponse({ code: "request_id_replay_conflict" }, 409);
      }
    }
    if (path === "/h3-context/v1/production/action") {
      productionActions.push(action);
      if (
        action === "release_workspace" ||
        action === "release_generation_destination" ||
        action === "release_generation_destination_v2"
      )
        return new Response(null, { status: 204 });
      if (action === "admit_generation_destination_v2") {
        const targetHandle = payload.workspace_handle;
        projectAttemptStatus = "admitted";
        if (targetHandle === null)
          return jsonResponse(accumulatedProject(), 201);
        if (
          typeof targetHandle === "string" &&
          accumulatedProject().workspace_handle === targetHandle &&
          accumulatedProject().workspace_id === payload.workspace_id
        )
          return jsonResponse(accumulatedProject());
        return jsonResponse({}, 410);
      }
      if (action === "read_accumulated_project")
        return jsonResponse(accumulatedProject());
      if (action === "read_projection" && projectProjection !== undefined)
        return jsonResponse(projectProjection);
      return jsonResponse(
        wires.initialProduction,
        action === "create_workspace_from_context" ? 201 : 200,
      );
    }

    if (path === "/h3-context/v1/authoring/action") {
      authoringActions.push(action);
      if (action === "create_authoring_workspace")
        return jsonResponse(preparedAuthoringProjection, 201);
      // M25-40: import prepares through the live Production owner. The backend adopts the
      // preferred same-lineage editor this journey created from Context with 200.
      if (action === "ensure_authoring_from_production")
        return jsonResponse(preparedAuthoringProjection, 200);
      if (
        action === "initialize_timeline_history" ||
        action === "read_timeline_history"
      )
        return jsonResponse(currentHistory());
      if (action === "release_workspace")
        return new Response(null, { status: 204 });
      return jsonResponse({}, 422);
    }

    if (path === "/h3-context/v1/production/authoring-import") {
      importCount += 1;
      const response = structuredClone(importResponseWire()) as JsonRecord;
      const receipt = object(response.receipt)!;
      const requestEntries = Array.isArray(body.entries)
        ? (body.entries as JsonRecord[])
        : [];
      const responseAuthoring = object(response.authoring_projection)!;
      const responseReference = object(responseAuthoring.reference)!;
      const receiptReference = object(receipt.reference)!;
      receipt.request_id = body.request_id;
      receipt.production_workspace_id = body.production_workspace_id;
      receipt.production_workspace_revision =
        body.expected_production_workspace_revision;
      receipt.production_workspace_fingerprint =
        body.expected_production_workspace_fingerprint;
      receipt.authoring_workspace_handle = body.authoring_workspace_handle;
      receipt.authoring_registry_fingerprint =
        body.expected_authoring_registry_fingerprint;
      receiptReference.prior_revision =
        body.expected_authoring_reference_revision;
      receiptReference.next_revision =
        Number(body.expected_authoring_reference_revision) + 1;
      receipt.rows = requestEntries.map((entry, index) => ({
        segment_id: entry.segment_id,
        output_handle: entry.output_handle,
        asset_id: `generated.asset.${index + 1}`,
        source_kind: "video",
        disposition: "created",
      }));
      for (const row of receipt.rows as JsonRecord[])
        if (!importedAssetIds.includes(String(row.asset_id)))
          importedAssetIds.push(String(row.asset_id));
      responseAuthoring.workspace_handle = body.authoring_workspace_handle;
      responseAuthoring.registry_fingerprint =
        body.expected_authoring_registry_fingerprint;
      responseReference.revision = receiptReference.next_revision;
      return jsonResponse(response);
    }

    if (path === "/h3-context/v1/generation/coordinator") {
      coordinatorActions.push(action);
      if (action === "prepare_managed_run") {
        const observation = object(payload.observation) ?? {};
        const correlation = object(payload.correlation) ?? {};
        const observedGraphFingerprint = observation.graph_fingerprint;
        const observedCompiledFingerprint =
          observation.compiled_prompt_fingerprint;
        const observedProductShellNodeId = correlation.execution_node_id;
        if (
          typeof observedGraphFingerprint !== "string" ||
          typeof observedCompiledFingerprint !== "string" ||
          observedProductShellNodeId !== productShellNodeId ||
          typeof correlation.prompt_id !== "string"
        )
          return jsonResponse({}, 422);
        memberRun = typeof payload.production_admission_request_id === "string";
        activeOrdinal = completedSegments + 1;
        generationAttemptOrdinal += 1;
        activePromptId = correlation.prompt_id;
        const managedDuration = {
          duration_milliseconds: duration.effective_milliseconds,
          delivered_milliseconds: duration.effective_milliseconds,
          frame_count: duration.frame_count,
          snapped: false,
        } as const;
        wires = memberRun
          ? accumulatedLifecycleWires(
              observedGraphFingerprint,
              observedCompiledFingerprint,
              productShellNodeId,
              activePromptId,
              managedDuration,
              activeOrdinal,
              generationAttemptOrdinal,
            )
          : managedLifecycleWires(
              observedGraphFingerprint,
              observedCompiledFingerprint,
              productShellNodeId,
              activePromptId,
              managedDuration,
            );
        projectAttemptStatus = "planned";
        if (typeof observation.expected_frames === "number")
          coordinatorExpectedFrames.push(observation.expected_frames);
        return jsonResponse(
          memberRun
            ? managedMemberCoordinatorResponse(
                "prepared",
                wires.planned,
                wires.production.planned,
                `segment_${activeOrdinal}`,
              )
            : managedCoordinatorResponse(
                "prepared",
                wires.planned,
                wires.production.planned,
              ),
          200,
        );
      }
      if (action === "submit_managed_run") {
        if (payload.queue_prompt_id !== activePromptId)
          return jsonResponse({}, 422);
        projectAttemptStatus = "submitted";
        return jsonResponse(
          memberRun
            ? managedMemberCoordinatorResponse(
                "submitted",
                wires.submitted,
                wires.production.submitted,
                `segment_${activeOrdinal}`,
              )
            : managedCoordinatorResponse(
                "submitted",
                wires.submitted,
                wires.production.submitted,
              ),
        );
      }
      if (action === "close_managed_run") {
        const artifact = object(payload.artifact);
        if (payload.queue_prompt_id !== activePromptId)
          return jsonResponse({}, 422);
        // CRITICAL: mirror the aggregate's own rule -- an artifact belongs to a success close and
        // to no other terminal. A double that answers 422 to every non-success close cannot tell a
        // client that wrongly forwards a buffered artifact from a client that correctly drops it,
        // so the contract mismatch stays invisible to every hermetic journey.
        if (payload.kind !== "success") {
          if (payload.artifact !== null)
            return jsonResponse(
              {
                schema: "h3.context.generation_coordinator.error.v1",
                category: "invalid_managed_artifact",
                retry_disposition: "none",
                same_run_authority: false,
              },
              400,
            );
          projectAttemptStatus =
            payload.kind === "interrupted" ? "cancelled" : "failed";
          return jsonResponse(
            memberRun
              ? managedMemberCoordinatorResponse(
                  payload.kind === "interrupted" ? "interrupted" : "failed",
                  wires.running,
                  wires.production.submitted,
                  `segment_${activeOrdinal}`,
                )
              : managedCoordinatorResponse(
                  payload.kind === "interrupted" ? "interrupted" : "failed",
                  wires.running,
                  wires.production.submitted,
                ),
          );
        }
        if (payload.artifact === null) {
          // CRITICAL: success without a SaveVideo event records the exact terminal and remains
          // verification-pending. Returning 422 here hides terminal loss and reconnect defects.
          successTerminalPendingArtifact = true;
          projectAttemptStatus = "output_verification_failed";
          return jsonResponse(
            memberRun
              ? managedMemberCoordinatorResponse(
                  "verification_pending",
                  wires.running,
                  wires.production.running,
                  `segment_${activeOrdinal}`,
                )
              : managedCoordinatorResponse(
                  "verification_pending",
                  wires.running,
                  wires.production.running,
                ),
          );
        }
        if (artifact?.output_node_id !== saveVideoNodeId)
          return jsonResponse({}, 422);
        if (options.artifactFailure === true)
          return jsonResponse(
            {
              schema: "h3.context.generation_coordinator.error.v1",
              category: "artifact_store_unavailable",
              retry_disposition: "retry_output_verification",
              same_run_authority: true,
            },
            503,
          );
        successTerminalPendingArtifact = false;
        completedSegments = activeOrdinal;
        rememberAccumulatedSegmentCount(completedSegments);
        projectProjection = wires.production.succeeded as JsonRecord;
        projectAttemptStatus = "succeeded";
        return jsonResponse(
          memberRun
            ? managedMemberCoordinatorResponse(
                "succeeded",
                wires.succeeded,
                wires.production.succeeded,
                `segment_${activeOrdinal}`,
              )
            : managedCoordinatorResponse(
                "succeeded",
                wires.succeeded,
                wires.production.succeeded,
              ),
        );
      }
      if (action === "read_managed_run") {
        if (reconcileRecord === "unknown")
          return jsonResponse(
            {
              schema: "h3.context.generation_coordinator.error.v1",
              category: "run_authority_mismatch",
              retry_disposition: "none",
              same_run_authority: false,
            },
            410,
          );
        const succeeded = reconcileRecord === "succeeded";
        return jsonResponse(
          memberRun
            ? managedMemberCoordinatorResponse(
                !succeeded && successTerminalPendingArtifact
                  ? "verification_pending"
                  : "current",
                succeeded ? wires.succeeded : wires.running,
                succeeded
                  ? wires.production.succeeded
                  : wires.production.running,
                `segment_${activeOrdinal}`,
              )
            : managedCoordinatorResponse(
                !succeeded && successTerminalPendingArtifact
                  ? "verification_pending"
                  : "current",
                succeeded ? wires.succeeded : wires.running,
                succeeded
                  ? wires.production.succeeded
                  : wires.production.running,
              ),
        );
      }
      return jsonResponse({}, 422);
    }
    return jsonResponse({}, 503);
  });

  await import("../src/entry");
  registeredExtension().setup?.();

  const panel = document.createElement("section");
  panel.className = "side-bar-panel";
  const content = document.createElement("div");
  content.className = "sidebar-content-container";
  const container = document.createElement("div");
  container.id = "managed-entry-sidebar";
  content.append(container);
  panel.append(content);
  root.replaceChildren(panel);
  registeredTab().render(container);

  window.h3ManagedEntryHarness = Object.freeze({
    snapshot: () => {
      const sideEffects = sideEffectCounts();
      return Object.freeze({
        queueCount: sideEffects.queues,
        queueNodeTypes: queueNodeTypes(),
        queueFrameCounts: queueFrameCounts(),
        durationResolutionRequests: durationResolutionRequests(),
        coordinatorExpectedFrames: [...coordinatorExpectedFrames],
        coordinatorActions: [...coordinatorActions],
        productionActions: [...productionActions],
        authoringActions: [...authoringActions],
        importCount,
        lifecycleEvents: lifecycleEvents(),
        managedRequestIds: [...managedRequestIds],
        requestLedgerIds: requestLedgerIds(),
        replayCollisionCount,
        queuePromptWrapperMode: options.queuePromptWrapperMode ?? "none",
        projectWorkspaceHandle:
          typeof accumulatedProject().workspace_handle === "string"
            ? (accumulatedProject().workspace_handle as string)
            : null,
        projectSegmentCount: Array.isArray(projectProjection?.segments)
          ? projectProjection.segments.length
          : 0,
      });
    },
    dispatchBootstrap: () =>
      dispatchProjection(productShellNodeId, {
        promptId:
          MANAGED_PROMPT_IDS[Math.max(0, sideEffectCounts().queues - 1)] ??
          activePromptId,
        workspaceId: wires.workspaceId,
        requestedSeconds: duration.requested_seconds,
        effectiveDurationMilliseconds: duration.effective_milliseconds,
        effectiveFrameCount: duration.frame_count,
        taskMode: "t2va",
      }),
    dispatchTerminal: () =>
      dispatchExecutionTerminal("success", activePromptId),
    dispatchFailure: () => dispatchExecutionTerminal("error", activePromptId),
    dispatchInterruption: () =>
      dispatchExecutionTerminal("interrupted", activePromptId),
    dispatchArtifact: () =>
      dispatchSaveVideoArtifact(activePromptId, saveVideoNodeId),
    setReconcileRecord: (record) => {
      reconcileRecord = record;
    },
    dropHostSocket: () => dispatchHostSocketDropped(),
    reconnectingHostSocket: () => dispatchHostReconnecting(),
    restoreHostSocket: () => dispatchHostReconnected(),
    destroySidebar: () => registeredTab().destroy(),
    renderSidebar: () => registeredTab().render(container),
  });
}
