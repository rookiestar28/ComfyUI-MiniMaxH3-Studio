import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  AppModeInputs,
  AppModeStartOptions,
  AppModeStartResult,
  AppModeCanvasResult,
  ManagedAppModePreparation,
} from "../src/host/appMode";
import { unavailableProductionAssemblyWire } from "./support/productionAssemblyWire";
import {
  AppModeError,
  fingerprint as appModeFingerprint,
} from "../src/host/appMode";
import {
  spliceContextPipeline,
  type Json,
} from "../src/host/templateMaterialization";
import {
  H3_SHELL_MANIFEST,
  inspectVisibleH3Graph,
} from "../src/host/graphAdapter";
import { decodeGenerationSequenceProjection } from "../src/contracts/generationSequenceCodec";
import {
  PROVIDER_SETTINGS_SCHEMA,
  decodeProviderSettingsProjection,
} from "../src/contracts/providerSettingsCodec";
import { providerCopy } from "../src/i18n/catalog";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import { productionAccumulatedProjectFingerprint } from "../src/contracts/productionAccumulationCodec";
import { generationSequenceWire } from "./generationSequenceFixture";
import { createNoisyHostExtension } from "./support/hostSeamTestDouble";
import { acceptedQueueReceipt } from "./support/queuePromptTestDouble";
import { loadSyntheticTemplate } from "./support/templateFixture";
import { observeOwnedGraph } from "../src/host/ownedGraphIdentity";

type AppModeStart = (
  inputs: AppModeInputs,
  options?: AppModeStartOptions,
) => Promise<AppModeStartResult>;

const appModeStartHarness = vi.hoisted(() => ({
  start: undefined as AppModeStart | undefined,
  workflow: undefined as (() => object | undefined) | undefined,
  prepare: undefined as
    | ((
        inputs: AppModeInputs,
        options?: AppModeStartOptions,
      ) => Promise<AppModeCanvasResult>)
    | undefined,
}));
const OWNED_RESULT_IDENTITY = Object.freeze({
  ownedProjectionFingerprint: `sha256:${"3".repeat(64)}`,
  ownedNodeIds: Object.freeze(["1", "8", "45"]),
  ownedLinkIds: Object.freeze(["15", "17", "18"]),
});

/**
 * The generation admission seam, scripted answer by answer.
 *
 * M17-20 D1 makes admission a backend decision the shell asks for, so how the
 * shell *remembers* the answer is its own behaviour and is what these rows
 * check.
 */
const appModeAdmissionHarness = vi.hoisted(() => ({
  answers: [] as { status: string; reason?: string }[],
  calls: 0,
}));

const appModeConnectHarness = vi.hoisted(() => ({
  candidates: {
    tier: "unavailable" as "connect" | "designate" | "unavailable",
    anchors: [] as Array<{
      nodeId: number;
      anchorType: string;
      nested: boolean;
      taskMode: string;
    }>,
  },
}));

/**
 * The seam the entry uses to declare a canvas write as App Mode's own.
 *
 * M17-20 D11 materializes through the host's graph-load seam, which makes the
 * host announce a configure to every extension -- including this one. The rows
 * below drive that announcement from inside and outside the declaration,
 * because only the real host fires it and the difference decides whether a run
 * survives its own materialization.
 */
const ownedConfigureHarness = vi.hoisted(() => ({
  begin: undefined as (() => () => void) | undefined,
}));

vi.mock("../src/styles/tokens.css?inline", () => ({
  default: ".h3c{}",
}));

/**
 * M25-21: a transparent observer of the session retention store each entry load creates, so the
 * whole-extension disposal row can read the entry's own store after its view is gone. Behaviour
 * is the real store's; only the instance is recorded.
 */
const retentionHarness = vi.hoisted(() => ({
  stores: [] as Array<{ size(): number; generation(): number }>,
}));

vi.mock("../src/state/sidebarRetention", async () => {
  const actual = await vi.importActual<
    typeof import("../src/state/sidebarRetention")
  >("../src/state/sidebarRetention");
  return {
    ...actual,
    createSidebarRetention: () => {
      const store = actual.createSidebarRetention();
      retentionHarness.stores.push(store);
      return store;
    },
  };
});

vi.mock("../src/host/appMode", async () => {
  const actual = await vi.importActual<typeof import("../src/host/appMode")>(
    "../src/host/appMode",
  );
  return {
    ...actual,
    createAppModeController: (
      app: {
        graph?: { serialize?: () => unknown };
      },
      _api: unknown,
      controllerOptions: {
        beginOwnedGraphConfigure?: () => () => void;
      } = {},
    ) => ({
      capability: () => {
        ownedConfigureHarness.begin =
          controllerOptions.beginOwnedGraphConfigure;
        return { status: "ready" as const };
      },
      admission: async () => {
        appModeAdmissionHarness.calls += 1;
        return (appModeAdmissionHarness.answers.shift() ?? {
          status: "admitted" as const,
        }) as never;
      },
      imageSources: () => [],
      mediaSources: () => [],
      activeWorkflow: () =>
        appModeStartHarness.workflow === undefined
          ? app.graph
          : appModeStartHarness.workflow(),
      connectCandidates: () => appModeConnectHarness.candidates as never,
      prepareCanvas: async (
        inputs: AppModeInputs,
        options: AppModeStartOptions = {},
      ) => {
        if (appModeStartHarness.prepare !== undefined)
          return appModeStartHarness.prepare(inputs, options);
        const serialized = app.graph?.serialize?.();
        return {
          preparedOnly: true as const,
          ...OWNED_RESULT_IDENTITY,
          transactionId: options.transactionId,
          graphFingerprint: actual.fingerprint(serialized),
          compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
          route: options.replaceExisting
            ? ("replace" as const)
            : ("new" as const),
        };
      },
      start: async (
        inputs: AppModeInputs,
        options: AppModeStartOptions = {},
      ) => {
        if (appModeStartHarness.start !== undefined) {
          const result = await appModeStartHarness.start(inputs, options);
          return { ...result, transactionId: options.transactionId };
        }
        const serialized = app.graph?.serialize?.();
        if (serialized === undefined)
          throw new Error("test graph is unavailable");
        return {
          queueResult: acceptedQueueReceipt("prompt-1"),
          ...OWNED_RESULT_IDENTITY,
          transactionId: options.transactionId,
          graphFingerprint: actual.fingerprint(serialized),
          compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
          queuePromptId: "prompt-1",
          route: "replace" as const,
        };
      },
    }),
  };
});

type EntryHostFixture = typeof import("./fixtures/entryHostModules");
type EntryHostSettings = EventTarget & {
  getSettingValue(id: string): unknown;
  setSettingValue(id: string, value: unknown): void;
  setSettingValueAsync(id: string, value: unknown): Promise<void>;
};

function createEntryHostSettings(): EntryHostSettings {
  const values = new Map<string, unknown>([
    ["H3.Context.Language", "auto"],
    ["Comfy.Locale", "en"],
  ]);
  const settings = new EventTarget() as EntryHostSettings;
  settings.getSettingValue = (id) => values.get(id);
  settings.setSettingValue = (id, value) => values.set(id, value);
  settings.setSettingValueAsync = async (id, value) => {
    values.set(id, value);
  };
  return settings;
}

function productionProjectionWire(
  revision = 1,
  segmentCount = 1,
): Record<string, unknown> {
  return {
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: `pw_${"p".repeat(43)}`,
    workspace_id: "workspace_1",
    workspace_revision: revision,
    workspace_fingerprint: `sha256:${"a".repeat(64)}`,
    segments: Array.from({ length: segmentCount }, (_, index) => ({
      segment_id: `segment_${index + 1}`,
      ordinal: index + 1,
      task_mode: "t2va",
      duration: {
        duration_milliseconds: 5000,
        delivered_milliseconds: 5167,
        frame_count: 124,
        snapped: true,
      },
      relation: "independent",
      predecessor_segment_id: null,
      boundary_kind: "independent",
      closure_state: "unavailable",
      job_state: "unavailable",
      artifact_state: "unavailable",
      continuity_state: "unavailable",
      delivered_geometry: null,
    })),
    selected_segment_ids: Array.from(
      { length: segmentCount },
      (_, index) => `segment_${index + 1}`,
    ),
    run: { state: "unavailable", completed: 0, total: segmentCount },
    generation_sequence: null,
    reconstruction: { state: "unavailable" },
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: [],
    outputs: [],
    allowed_actions: [
      "add_segment_from_context",
      "replace_segment_from_context",
      "set_segment_relation",
      "set_selection",
      "read_projection",
      "release_workspace",
    ],
    blocker_codes: ["sequence_authority_unavailable"],
    limits: { max_segments: 64, max_outputs: 65 },
  };
}

function providerProjectionWire(
  credentialPresent: boolean,
): Record<string, unknown> {
  const wire = {
    schema: PROVIDER_SETTINGS_SCHEMA,
    revision: 1,
    catalog_empty: false,
    profiles: [
      {
        profile_id: "remote.example.gpt",
        provider_label: "OpenAI",
        family: "remote_openai_compatible",
        wire_dialect: "openai_chat_completions",
        adapter_version: "1.0.0",
        parser_version: "h3.prompt_model.draft_json.v1",
        cost_class: "paid_remote",
        usage_receipt_required: true,
        retention_policy: "provider_policy",
        qualification_state: "catalog_only",
        limitations: ["remote_activation_pending"],
        host: "api.example.com",
        port: 443,
      },
    ],
    selected_profile_id: "remote.example.gpt",
    selected_model_id: "",
    selected_model: null,
    readiness: "unreachable",
    disclosure: {
      family: "remote_openai_compatible",
      destination: "internet",
      transfer_boundary: "remote_upload",
      preflight_required: true,
      consent_required: true,
      local_only: false,
      requires_credential: true,
      accepted_media: ["text"],
      transmits_media: false,
      consent_scope: "session_only",
      provider_id: "openai",
      retention_policy: "provider_policy",
    },
    consent: null,
    consent_required: true,
    credential_required: true,
    credential_present: credentialPresent,
    credential_last_four: "",
    candidates: [],
    candidates_truncated: false,
    diagnostic: null,
    reachability_observed: true,
    assisted_authoring: {
      available: true,
      selected: true,
      ready: false,
      authorized_for_this_action: false,
      defaulted: false,
    },
  };
  // IMPORTANT: stale envelopes must fail here, before they hide credential-cleanup assertions behind an unavailable UI.
  decodeProviderSettingsProjection(wire);
  return wire;
}

function productionPreviewProjectionWire(): Record<string, unknown> {
  const wire = productionProjectionWire();
  wire.reconstruction = { state: "complete" };
  wire.authority_versions = ["h3.context.av_reconstruction_receipt.v1"];
  wire.outputs = [
    {
      output_handle: `out_${"c".repeat(40)}`,
      ordinal: 1,
      state: "ready",
      segment_id: null,
      preview: true,
    },
  ];
  wire.allowed_actions = [
    ...(wire.allowed_actions as string[]),
    "preview_output",
  ];
  return wire;
}

function productionClipPreviewProjectionWire(): Record<string, unknown> {
  const wire = productionPreviewProjectionWire();
  (wire.outputs as Array<Record<string, unknown>>).push({
    output_handle: `out_${"d".repeat(40)}`,
    ordinal: 2,
    state: "ready",
    segment_id: "segment_1",
    preview: true,
  });
  return wire;
}

function productionClipJourneyProjectionWire(
  revision: number,
  selectedSegmentId: "segment_1" | "segment_2",
): Record<string, unknown> {
  const wire = productionProjectionWire(revision, 2);
  wire.selected_segment_ids = [selectedSegmentId];
  wire.reconstruction = { state: "complete" };
  wire.authority_versions = ["h3.context.av_reconstruction_receipt.v1"];
  wire.outputs = [
    {
      output_handle: `out_${"c".repeat(40)}`,
      ordinal: 1,
      state: "ready",
      segment_id: null,
      preview: true,
    },
    {
      output_handle: `out_${"d".repeat(40)}`,
      ordinal: 2,
      state: "ready",
      segment_id: "segment_1",
      preview: true,
    },
    {
      output_handle: `out_${"e".repeat(40)}`,
      ordinal: 3,
      state: "ready",
      segment_id: "segment_2",
      preview: true,
    },
  ];
  wire.allowed_actions = [
    ...(wire.allowed_actions as string[]),
    "preview_output",
  ];
  return wire;
}

function previewResponse(payload = new Uint8Array([0, 1, 2, 3])): Response {
  let offset = 0;
  const stream = new ReadableStream<Uint8Array>({
    type: "bytes" as const,
    pull(controller) {
      const byteController =
        controller as unknown as ReadableByteStreamController;
      const request = byteController.byobRequest;
      if (request === null) throw new Error("BYOB request required");
      const view = request.view;
      if (view === null) throw new Error("BYOB view required");
      new Uint8Array(view.buffer, view.byteOffset, 1)[0] = payload[offset++]!;
      request.respond(1);
      if (offset === payload.length) byteController.close();
    },
  } as UnderlyingSource<Uint8Array>);
  return new Response(stream, {
    status: 200,
    headers: {
      "Content-Type": "video/mp4",
      "Content-Length": String(payload.length),
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "Content-Disposition": 'inline; filename="h3-preview.mp4"',
    },
  });
}

const semanticHash = (value: string) => `sha256:${value.repeat(64)}`;

function semanticProposalHandleWire(): Record<string, unknown> {
  return {
    schema: "h3.context.semantic_proposal_review_handle.v1",
    review_id: `review_${"r".repeat(32)}`,
    transaction_fingerprint: semanticHash("b"),
    workspace_fingerprint: semanticHash("c"),
    available: true,
    reason: "review_available",
  };
}

function semanticProposalResultWire(
  reportFingerprint: string,
  terminal: null | "accepted" | "rejected" = null,
  summary = "Entry-owned bounded proposal summary",
): Record<string, unknown> {
  return {
    schema: "h3.context.semantic_proposal.action_result.v1",
    outcome: terminal === null ? "read" : terminal,
    reason: terminal === null ? "review_current" : `proposal_${terminal}`,
    review: {
      schema: "h3.context.semantic_proposal_review.v1",
      review_id: `review_${"r".repeat(32)}`,
      transaction_fingerprint: semanticHash("b"),
      workspace_id: "proposal.workspace",
      workspace_revision: 1,
      workspace_fingerprint: semanticHash("c"),
      report_fingerprint: reportFingerprint,
      attempt: 1,
      revision: terminal === null ? 1 : 2,
      state: terminal === null ? "ready_for_review" : terminal,
      segment_id: "source.segment",
      correlation: { prompt_id: "prompt-1", execution_node_id: "17" },
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
      reason_code: terminal === null ? "review_ready" : "proposal_accepted",
      actions: {
        proposal_read: true,
        proposal_resolve: false,
        proposal_accept: terminal === null,
        proposal_reject: terminal === null,
        proposal_cancel: terminal === null,
        edit: false,
        regenerate: false,
      },
      action_reasons: {
        edit: "source_owner_unavailable",
        regenerate: "source_owner_unavailable",
      },
      terminal,
    },
  };
}

function resolvableProposalResultWire(
  reportFingerprint: string,
): Record<string, unknown> {
  const result = semanticProposalResultWire(reportFingerprint);
  const review = result.review as Record<string, unknown>;
  review.state = "clarification_required";
  review.reason_code = "clarification_required";
  review.clarifications = [
    {
      clarification_id: "clarification.safe",
      label: "Synthetic camera choice",
      reason_code: "resolution_required",
    },
  ];
  review.uncertainty_codes = ["clarification_required"];
  review.actions = {
    ...(review.actions as Record<string, unknown>),
    proposal_resolve: true,
    proposal_accept: false,
  };
  return result;
}

function resolvedProposalResultWire(
  reportFingerprint: string,
): Record<string, unknown> {
  const result = semanticProposalResultWire(reportFingerprint);
  result.outcome = "resolved";
  result.reason = "proposal_resolved";
  const review = result.review as Record<string, unknown>;
  review.revision = 2;
  review.workspace_revision = 2;
  return result;
}

type EntryGenerationSequenceWire = ReturnType<typeof generationSequenceWire>;

function entryGenerationSequenceWire(
  state = "b",
  attempt = 1,
  graphFingerprint = `sha256:${"1".repeat(64)}`,
): EntryGenerationSequenceWire {
  const wire = generationSequenceWire();
  Object.assign(wire, {
    sequence_id: "sequence.1",
    sequence_fingerprint: `sha256:${"d".repeat(64)}`,
    state_fingerprint: `sha256:${state.repeat(64)}`,
    workspace_id: "workspace_1",
    workspace_revision: 1,
    workspace_fingerprint: `sha256:${"a".repeat(64)}`,
    correlation: { prompt_id: "prompt-1", execution_node_id: "17" },
  });
  Object.assign(wire.progress[0]!, {
    job_id: "job.1",
    segment_id: "segment_1",
    ordinal: 1,
    state: "planned",
    attempt,
    transaction_id: `generation.command.${attempt}`,
  });
  Object.assign(wire.eligible_commands[0]!, {
    job_id: "job.1",
    segment_id: "segment_1",
    ordinal: 1,
    task_mode: "t2va",
    source_id: "report-1",
    reference_ids: [],
    // IMPORTANT: the managed Production command is seeded from the effective
    // frame count, not the authored seconds. Mirroring 5000 ms here fabricates
    // an authority shape the backend never returns for a 124-frame workspace.
    duration: {
      duration_milliseconds: 5167,
      frame_count: 124,
      delivered_milliseconds: 5167,
      snapped: false,
    },
    graph_fingerprint: graphFingerprint,
    compiled_prompt_fingerprint: `sha256:${"2".repeat(64)}`,
    attempt,
    transaction_id: `generation.command.${attempt}`,
  });
  return wire;
}

function generationReadyProductionWire(
  sequence: EntryGenerationSequenceWire,
  promptId = "prompt-1",
  commandSource = sequence,
): Record<string, unknown> {
  const projection = productionProjectionWire();
  const segment = (projection.segments as Array<Record<string, unknown>>)[0]!;
  const command = commandSource.eligible_commands[0];
  if (command !== undefined) {
    Object.assign(segment, {
      segment_id: command.segment_id,
      ordinal: command.ordinal,
      task_mode: command.task_mode,
      duration: structuredClone(command.duration),
    });
    projection.selected_segment_ids = [command.segment_id];
  }
  Object.assign(segment, {
    closure_state: "dirty_self",
    job_state: "planned",
  });
  Object.assign(projection, {
    workspace_id: sequence.workspace_id,
    workspace_revision: sequence.workspace_revision,
    workspace_fingerprint: sequence.workspace_fingerprint,
    run: { state: "ready", completed: 0, total: 1 },
    generation_sequence: {
      schema: sequence.schema,
      sequence_id: sequence.sequence_id,
      sequence_fingerprint: sequence.sequence_fingerprint,
      state_fingerprint: sequence.state_fingerprint,
      workspace_id: sequence.workspace_id,
      workspace_revision: sequence.workspace_revision,
      workspace_fingerprint: sequence.workspace_fingerprint,
      correlation: { prompt_id: promptId, execution_node_id: "17" },
    },
    authority_versions: [sequence.schema],
    allowed_actions: [
      ...(projection.allowed_actions as string[]),
      "submit_generation_job",
    ],
    blocker_codes: [],
  });
  return projection;
}

function managedSequenceStateWire(
  planned: EntryGenerationSequenceWire,
  state:
    | "planned"
    | "submitted"
    | "running"
    | "output_verification_failed"
    | "succeeded"
    | "failed",
): EntryGenerationSequenceWire {
  const sequence = structuredClone(planned);
  const progress = sequence.progress[0]!;
  sequence.state_fingerprint = `sha256:${(
    {
      planned: "b",
      submitted: "c",
      running: "d",
      output_verification_failed: "f",
      succeeded: "e",
      failed: "g",
    } as const
  )[state].repeat(64)}`;
  progress.state = state;
  if (state !== "planned") {
    sequence.eligible_commands = [];
    Object.assign(progress, {
      transaction_id: "generation.command.1",
      queue_prompt_id: "prompt.model.1",
    });
  }
  if (
    state === "running" ||
    state === "output_verification_failed" ||
    state === "succeeded" ||
    state === "failed"
  )
    Object.assign(progress, { host_owner_id: "prompt.model.1" });
  if (state === "output_verification_failed")
    Object.assign(progress, { failure_code: "artifact_store_unavailable" });
  if (state === "failed")
    Object.assign(progress, { failure_code: "execution_failed" });
  if (state === "succeeded") {
    sequence.complete = true;
    Object.assign(progress, {
      artifact_receipt_fingerprint: `sha256:${"6".repeat(64)}`,
      artifact_output_fingerprint: `sha256:${"7".repeat(64)}`,
    });
  }
  return sequence;
}

function managedProductionStateWire(
  sequence: EntryGenerationSequenceWire,
  state:
    | "planned"
    | "submitted"
    | "running"
    | "output_verification_failed"
    | "succeeded"
    | "failed",
  commandSource = sequence,
): Record<string, unknown> {
  const production = generationReadyProductionWire(
    sequence,
    "prompt.bootstrap.1",
    commandSource,
  );
  const segment = (production.segments as Array<Record<string, unknown>>)[0]!;
  segment.job_state = state;
  segment.closure_state = state === "succeeded" ? "clean" : "dirty_self";
  segment.artifact_state = state === "succeeded" ? "complete" : "unavailable";
  production.run = {
    state:
      state === "planned"
        ? "ready"
        : state === "output_verification_failed" || state === "failed"
          ? "failed"
          : state === "succeeded"
            ? "succeeded"
            : "running",
    completed:
      state === "succeeded" ||
      state === "output_verification_failed" ||
      state === "failed"
        ? 1
        : 0,
    total: 1,
  };
  production.allowed_actions = (production.allowed_actions as string[]).filter(
    (action) => action !== "submit_generation_job",
  );
  if (state === "planned")
    (production.allowed_actions as string[]).push("submit_generation_job");
  if (state === "succeeded") {
    production.authority_versions = [
      "h3.context.generation_sequence_projection.v1",
      "h3.context.segment_artifact_receipt.v1",
    ];
    production.outputs = [
      {
        output_handle: `out_${"o".repeat(24)}`,
        ordinal: 1,
        state: "ready",
        segment_id: segment.segment_id,
        preview: true,
      },
    ];
    (production.allowed_actions as string[]).push("preview_output");
  }
  return production;
}

function managedCoordinatorResponse(
  disposition:
    | "prepared"
    | "submitted"
    | "artifact_verified"
    | "verification_pending"
    | "output_verification_failed"
    | "succeeded"
    | "failed"
    | "interrupted"
    | "current"
    | "released",
  sequence: EntryGenerationSequenceWire,
  production: Record<string, unknown>,
): Record<string, unknown> {
  return {
    schema: "h3.context.generation_coordinator.response.v1",
    run_handle: `mc_${"m".repeat(40)}`,
    disposition,
    artifact_authority: null,
    terminal_fingerprint: null,
    sequence,
    production,
  };
}

function managedEntryFixture(
  graphFingerprint: string,
  workflowAuthority: object,
  serializedGraph: unknown,
  duration: Readonly<{
    effectiveMilliseconds: number;
    frameCount: number;
  }> = { effectiveMilliseconds: 8000, frameCount: 192 },
): Readonly<{
  workspaceId: string;
  planned: EntryGenerationSequenceWire;
  production: Record<string, unknown>;
  preparation: ManagedAppModePreparation;
}> {
  const ownedReference = Object.freeze({
    nodeIds: Object.freeze([] as string[]),
    linkIds: Object.freeze([] as string[]),
    anchorNodeId: "17",
    authoredWidgetNodeIds: Object.freeze([] as string[]),
  });
  const ownedObservation = observeOwnedGraph(serializedGraph, ownedReference);
  const workspaceId = `ws_${"m".repeat(40)}`;
  const productionWorkspaceId = `workspace_${"m".repeat(24)}`;
  const planned = entryGenerationSequenceWire("b", 1, graphFingerprint);
  // CRITICAL: Context and Production are separate authority namespaces. Keeping
  // this fixture aliased hid the supported-host failure that M23-17 repairs.
  planned.workspace_id = productionWorkspaceId;
  planned.correlation = {
    prompt_id: "prompt.bootstrap.1",
    execution_node_id: "17",
  };
  Object.assign(planned.eligible_commands[0]!.duration, {
    duration_milliseconds: duration.effectiveMilliseconds,
    delivered_milliseconds: duration.effectiveMilliseconds,
    frame_count: duration.frameCount,
    snapped: false,
  });
  planned.eligible_commands[0]!.expected_shape = [
    duration.frameCount,
    512,
    512,
    3,
  ];
  const production = productionProjectionWire();
  production.workspace_id = productionWorkspaceId;
  const initialSegment = (
    production.segments as Array<Record<string, unknown>>
  )[0]!;
  initialSegment.duration = {
    duration_milliseconds: duration.effectiveMilliseconds,
    delivered_milliseconds: duration.effectiveMilliseconds,
    frame_count: duration.frameCount,
    snapped: false,
  };
  production.run = { state: "unavailable", completed: 0, total: 0 };
  const preparation = Object.freeze<ManagedAppModePreparation>({
    bootstrap: {
      output: {
        "17": {
          class_type: "comfyui_h3_context.H3Context.ProductShell",
          inputs: {},
        },
      },
      workflow: {
        last_node_id: 0,
        last_link_id: 0,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
        version: 0.4,
      },
    },
    observation: {
      schema: "h3.context.prepared_graph_observation.v4",
      route: "existing",
      graph_fingerprint: graphFingerprint,
      compiled_prompt_fingerprint:
        planned.eligible_commands[0]!.compiled_prompt_fingerprint,
      owned_projection_fingerprint: ownedObservation.fingerprint,
      owned_node_ids: ownedObservation.nodeIds,
      owned_link_ids: ownedObservation.linkIds,
      model_fingerprint: planned.eligible_commands[0]!.model_fingerprint,
      runtime_fingerprint: planned.eligible_commands[0]!.runtime_fingerprint,
      fingerprint_domain: "output_producing_graph",
      expected_frames: duration.frameCount,
      source_identity: null,
      timeout_ms: 60_000,
      native_anchor_node_id: "6",
    },
    productShellNodeId: "17",
    nativeAnchorNodeId: "6",
    managedIdentity: Object.freeze({ workflowAuthority, ownedReference }),
  });
  return {
    workspaceId,
    planned,
    production,
    preparation,
  };
}

function deferredAppModeStart(): Readonly<{
  promise: Promise<AppModeStartResult>;
  resolve(value: AppModeStartResult): void;
  reject(error: Error): void;
}> {
  let resolve!: (value: AppModeStartResult) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<AppModeStartResult>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

function appModeResult(
  sequence: EntryGenerationSequenceWire,
): AppModeStartResult {
  const command = sequence.eligible_commands[0]!;
  return {
    queueResult: acceptedQueueReceipt(`generation-prompt-${command.attempt}`),
    ...OWNED_RESULT_IDENTITY,
    graphFingerprint: command.graph_fingerprint,
    compiledPromptFingerprint: command.compiled_prompt_fingerprint,
    queuePromptId: `generation-prompt-${command.attempt}`,
    route: "replace",
  };
}

async function clickReadyAppModeStart(): Promise<void> {
  let button: HTMLButtonElement | null = null;
  await waitFor(() => {
    button = document.querySelector<HTMLButtonElement>(
      '[data-h3-focus-key="app-submit"]',
    );
    expect(button).not.toBeNull();
    expect(button?.disabled).toBe(false);
  });
  fireEvent.click(button!);
}

function compatibleExistingGraph(): Json {
  return spliceContextPipeline(
    loadSyntheticTemplate("video_minimax_h3_t2v") as Json,
    {
      taskMode: "t2va",
      userIntent:
        "A red kite crosses the sky while the camera follows its arc.",
      durationSeconds: 5.167,
    },
  ).workflow;
}

async function loadGenerationReadyEntry(): Promise<{
  fixture: EntryHostFixture;
  container: HTMLElement;
  sequence: EntryGenerationSequenceWire;
  publish(sequence: EntryGenerationSequenceWire, promptId: string): void;
}> {
  let production: Record<string, unknown> | undefined;
  const { fixture, container } = await loadEntry(async (_path, init) => {
    const body = JSON.parse(String(init.body)) as { action: string };
    if (body.action === "release_workspace")
      return {
        ok: true,
        status: 204,
        json: async () => ({}),
      };
    return {
      ok: true,
      status: body.action === "create_workspace_from_context" ? 201 : 200,
      json: async () => production ?? {},
    };
  });
  const sequence = entryGenerationSequenceWire(
    "b",
    1,
    appModeFingerprint(fixture.app.graph.serialize()),
  );
  production = generationReadyProductionWire(sequence);

  await clickReadyAppModeStart();
  await waitFor(() =>
    expect(
      container.querySelector('[data-shell-status="working"]'),
    ).not.toBeNull(),
  );
  act(() => fixture.dispatchProjection("17", { generationSequence: sequence }));
  await waitFor(() =>
    expect(
      container.querySelector('[data-shell-status="projected"]'),
    ).not.toBeNull(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Production" }));
  await screen.findByRole("button", { name: "Generate segment 1" });

  return {
    fixture,
    container,
    sequence,
    publish(nextSequence, promptId) {
      production = generationReadyProductionWire(nextSequence, promptId);
      act(() =>
        fixture.dispatchProjection("17", {
          promptId,
          generationSequence: nextSequence,
        }),
      );
    },
  };
}

async function loadEntry(
  productionHandler?: Parameters<EntryHostFixture["setFetchApiHandler"]>[0],
  initialGraph?: Record<string, unknown>,
  beforeImport?: (fixture: EntryHostFixture) => void,
): Promise<{
  fixture: EntryHostFixture;
  container: HTMLElement;
  hostSettings: EntryHostSettings;
}> {
  vi.resetModules();
  const fixture = await import("./fixtures/entryHostModules");
  fixture.setFetchApiHandler(productionHandler);
  const hostSettings = createEntryHostSettings();
  (
    fixture.app as typeof fixture.app & {
      ui?: { settings: EntryHostSettings };
    }
  ).ui = { settings: hostSettings };
  fixture.setGraph(initialGraph ?? fixture.canonicalGraph(17));
  beforeImport?.(fixture);
  await import("../src/entry");
  const extension = fixture.registeredExtension();
  act(() => extension.setup?.());

  const panel = document.createElement("section");
  panel.className = "side-bar-panel";
  const content = document.createElement("div");
  content.className = "sidebar-content-container";
  const container = document.createElement("div");
  content.append(container);
  panel.append(content);
  document.body.append(panel);
  act(() => fixture.registeredTab().render(container));

  return { fixture, container, hostSettings };
}

async function loadProjectedEntry(requestedSeconds = 5): Promise<{
  fixture: EntryHostFixture;
  container: HTMLElement;
}> {
  const { fixture, container } = await loadEntry();

  await clickReadyAppModeStart();
  await waitFor(() =>
    expect(
      container.querySelector('[data-shell-status="working"]'),
    ).not.toBeNull(),
  );
  act(() => fixture.dispatchProjection("17", { requestedSeconds }));
  await waitFor(() =>
    expect(
      container.querySelector('[data-shell-status="projected"]'),
    ).not.toBeNull(),
  );

  fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Prompt revision" }), {
    target: { value: "Synthetic projected draft" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Revision reason" }), {
    target: { value: "Synthetic projected reason" },
  });
  screen.getByRole("textbox", { name: "Revision reason" }).focus();
  return { fixture, container };
}

/**
 * Put a value into the credential field and keep it there.
 *
 * HC-18: `ProviderSettingsSection` clears an unsent key whenever the projection
 * context it was typed under could have changed, and that effect also runs once
 * on mount. `findByLabelText` resolves as soon as the DOM commit lands, which
 * can be before that passive effect has flushed; the effect then settles after
 * the change event's state update and wipes the value that was just typed, so
 * the arrange step fails with an empty field. Measured on a failing run the
 * field was enabled, still the same node, and empty synchronously after
 * `fireEvent.change` — nothing had remounted and no request was in flight.
 *
 * The retry is deliberately structural rather than a flush of pending effects.
 * A flush would only be correct if that reading of the interleaving is exactly
 * right, and the failure reproduces roughly once in twenty-five full-suite runs,
 * which is too rare to confirm one. Re-typing until the value sticks is correct
 * whatever queued work clears it, and on the ordinary path it succeeds on the
 * first iteration and costs nothing. A real browser cannot reach this state at
 * all: effects flush before paint, so no user can type into the gap.
 */
async function typeCredential(value: string): Promise<HTMLInputElement> {
  const field = (await screen.findByLabelText("API key")) as HTMLInputElement;
  await waitFor(() => {
    fireEvent.change(field, { target: { value } });
    expect(field.value).toBe(value);
  });
  return field;
}

describe("entry provider settings failure lifecycle", () => {
  it("presents an initial route failure and retries through the same seam", async () => {
    const paths: string[] = [];
    const intents: string[] = [];
    const handles: string[] = [];
    await loadEntry(async (path, init) => {
      paths.push(path);
      if (path === "/h3-context/v1/provider/settings") {
        const body = JSON.parse(String(init.body)) as { intent: string };
        intents.push(body.intent);
        const headers = init.headers as Record<string, string>;
        handles.push(headers["X-H3-Provider-Session"]);
        expect(String(init.body)).not.toContain(
          headers["X-H3-Provider-Session"],
        );
      }
      return {
        ok: false,
        status: 503,
        json: async () => ({}),
      };
    });

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    expect((await screen.findByRole("alert")).textContent).toContain(
      "Provider settings could not be loaded. The current state could not be confirmed.",
    );
    expect(
      paths.filter((path) => path === "/h3-context/v1/provider/settings"),
    ).toHaveLength(1);
    expect(intents).toEqual(["read_projection"]);

    fireEvent.click(
      screen.getByRole("button", {
        name: "Refresh models and check readiness",
      }),
    );
    await waitFor(() =>
      expect(
        paths.filter((path) => path === "/h3-context/v1/provider/settings"),
      ).toHaveLength(2),
    );
    expect(intents).toEqual(["read_projection", "recheck_readiness"]);
    expect(new Set(handles).size).toBe(1);
    expect(handles[0]).toMatch(/^ps_[0-9a-f]{32}$/);
    expect(
      window.sessionStorage.getItem("h3.context.provider.session_handle.v1"),
    ).toBe(handles[0]);
  });

  it("best-effort releases the same browser authority on pagehide", async () => {
    const releases: RequestInit[] = [];
    await loadEntry(async (path, init) => {
      if (
        path === "/h3-context/v1/provider/settings" &&
        init.method === "DELETE"
      )
        releases.push(init);
      return {
        ok: true,
        status: init.method === "DELETE" ? 204 : 200,
        json: async () => ({}),
      };
    });
    const releasedHandle = window.sessionStorage.getItem(
      "h3.context.provider.session_handle.v1",
    );
    window.dispatchEvent(new Event("pagehide"));
    await waitFor(() => expect(releases.length).toBeGreaterThan(0));
    const release = releases[0];
    const headers = release.headers as Record<string, string>;
    expect(release.body).toBeUndefined();
    expect(release.keepalive).toBe(true);
    expect(headers["X-H3-Provider-Session"]).toBe(releasedHandle);
    expect(
      window.sessionStorage.getItem("h3.context.provider.session_handle.v1"),
    ).not.toBe(releasedHandle);
  });

  it("clears an unsent key synchronously on pagehide without another intent", async () => {
    await loadEntry(async (path, init) => {
      if (path !== "/h3-context/v1/provider/settings")
        return { ok: false, status: 503, json: async () => ({}) };
      if (init.method === "DELETE")
        return { ok: true, status: 204, json: async () => ({}) };
      return {
        ok: true,
        status: 200,
        json: async () => ({
          schema: PROVIDER_SETTINGS_SCHEMA,
          accepted: true,
          rejection: null,
          projection: providerProjectionWire(false),
        }),
      };
    });

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    const field = await typeCredential("test-credential-pagehide");
    expect(field.value).toBe("test-credential-pagehide");

    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(field.value).toBe("");
    expect(document.body.innerHTML).not.toContain("test-credential-pagehide");
  });

  it("clears an unsent key synchronously across pagehide and a pending intent", async () => {
    let resolveRecheck:
      | ((value: {
          ok: boolean;
          status: number;
          json(): Promise<Record<string, unknown>>;
        }) => void)
      | undefined;
    const { fixture, container } = await loadEntry(async (path, init) => {
      if (path !== "/h3-context/v1/provider/settings")
        return { ok: false, status: 503, json: async () => ({}) };
      if (init.method === "DELETE")
        return { ok: true, status: 204, json: async () => ({}) };
      const body = JSON.parse(String(init.body)) as { intent: string };
      if (body.intent === "connect_and_refresh")
        return new Promise((resolve) => {
          resolveRecheck = resolve;
        });
      return {
        ok: true,
        status: 200,
        json: async () => ({
          schema: PROVIDER_SETTINGS_SCHEMA,
          accepted: true,
          rejection: null,
          projection: providerProjectionWire(false),
        }),
      };
    });

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    const field = await typeCredential("test-credential-unsent");
    fireEvent.click(
      screen.getByRole("button", {
        name: providerCopy("en").allowAndReload,
      }),
    );
    await waitFor(() => expect(resolveRecheck).toBeDefined());

    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(field.value).toBe("");
    act(() => window.dispatchEvent(new Event("pageshow")));
    const shownAgain = (await screen.findByLabelText(
      "API key",
    )) as HTMLInputElement;
    expect(shownAgain.value).toBe("");

    act(() =>
      resolveRecheck?.({
        ok: true,
        status: 200,
        json: async () => ({
          schema: PROVIDER_SETTINGS_SCHEMA,
          accepted: true,
          rejection: null,
          projection: providerProjectionWire(true),
        }),
      }),
    );
    await Promise.resolve();
    expect((screen.getByLabelText("API key") as HTMLInputElement).value).toBe(
      "",
    );

    act(() => fixture.registeredTab().destroy());
    act(() => fixture.registeredTab().render(container));
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    expect(
      ((await screen.findByLabelText("API key")) as HTMLInputElement).value,
    ).toBe("");
  });

  it("releases on normal unmount and remounts from a fresh projection", async () => {
    const posts: RequestInit[] = [];
    const releases: RequestInit[] = [];
    const { fixture, container } = await loadEntry(async (path, init) => {
      if (path === "/h3-context/v1/provider/settings") {
        if (init.method === "DELETE") {
          releases.push(init);
          return { ok: true, status: 204, json: async () => ({}) };
        }
        posts.push(init);
        return {
          ok: true,
          status: 200,
          json: async () => ({
            schema: PROVIDER_SETTINGS_SCHEMA,
            accepted: true,
            rejection: null,
            projection: providerProjectionWire(posts.length === 1),
          }),
        };
      }
      return { ok: false, status: 503, json: async () => ({}) };
    });

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    expect(
      await screen.findByText("Held for this browser session."),
    ).not.toBeNull();
    const firstHandle = (posts[0].headers as Record<string, string>)[
      "X-H3-Provider-Session"
    ];

    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    await waitFor(() => expect(releases).toHaveLength(1));
    expect(
      (releases[0].headers as Record<string, string>)["X-H3-Provider-Session"],
    ).toBe(firstHandle);

    act(() => tab.render(container));
    await waitFor(() => expect(posts).toHaveLength(2));
    const secondHandle = (posts[1].headers as Record<string, string>)[
      "X-H3-Provider-Session"
    ];
    expect(secondHandle).toMatch(/^ps_[0-9a-f]{32}$/);
    expect(secondHandle).not.toBe(firstHandle);
    expect(await screen.findByText("No credential is held.")).not.toBeNull();
    expect(screen.queryByText("Held for this browser session.")).toBeNull();
  });

  it("releases the provider session when the host reuses the mount without destroy", async () => {
    const posts: RequestInit[] = [];
    const releases: RequestInit[] = [];
    const { fixture, container } = await loadEntry(async (path, init) => {
      if (path === "/h3-context/v1/provider/settings") {
        if (init.method === "DELETE") {
          releases.push(init);
          return { ok: true, status: 204, json: async () => ({}) };
        }
        posts.push(init);
        return {
          ok: true,
          status: 200,
          json: async () => ({
            schema: PROVIDER_SETTINGS_SCHEMA,
            accepted: true,
            rejection: null,
            projection: providerProjectionWire(posts.length === 1),
          }),
        };
      }
      return { ok: false, status: 503, json: async () => ({}) };
    });

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    expect(
      await screen.findByText("Held for this browser session."),
    ).not.toBeNull();
    const firstHandle = (posts[0].headers as Record<string, string>)[
      "X-H3-Provider-Session"
    ];
    const replacement = document.createElement("section");
    replacement.dataset.foreignExtension = "";
    await act(async () => {
      container.replaceChildren(replacement);
      await Promise.resolve();
    });

    await waitFor(() => expect(releases).toHaveLength(1));
    expect(
      (releases[0].headers as Record<string, string>)["X-H3-Provider-Session"],
    ).toBe(firstHandle);

    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    expect(releases).toHaveLength(1);
    expect(container.firstChild).toBe(replacement);

    act(() => tab.render(container));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(
      (posts[1].headers as Record<string, string>)["X-H3-Provider-Session"],
    ).not.toBe(firstHandle);
    act(() => tab.destroy());
  });
});

describe("entry Production navigation availability", () => {
  it("keeps Production visible without authority and across graph configure", async () => {
    const { fixture } = await loadEntry();
    const production = await screen.findByRole("button", {
      name: "Production",
    });
    fireEvent.click(production);
    expect(
      screen.getByText("A ready Context workspace is required."),
    ).not.toBeNull();
    expect(
      screen.queryByRole("button", { name: "Create from current Context" }),
    ).toBeNull();
    expect(fixture.productionFetchBodies()).toHaveLength(0);

    const extension = fixture.registeredExtension() as {
      beforeConfigureGraph?: () => void;
    };
    act(() => extension.beforeConfigureGraph?.());
    expect(screen.getByRole("button", { name: "Production" })).not.toBeNull();
    expect(fixture.productionFetchBodies()).toHaveLength(0);
  });
});

describe("entry Production automatic workspace entry", () => {
  it("creates exactly once on the first eligible Production selection", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const { fixture } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      bodies.push(body);
      return {
        ok: true,
        status: 201,
        json: async () => productionProjectionWire(),
      };
    });
    act(() => fixture.dispatchProjection("17"));

    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });

    expect(bodies.map((body) => body.action)).toEqual([
      "create_workspace_from_context",
    ]);
    expect(
      screen.queryByRole("button", { name: "Create from current Context" }),
    ).toBeNull();
    expect(screen.queryByText("Production action in progress.")).toBeNull();
  });

  it("gives each browser session's first create a request id no other session issued", async () => {
    // The host keeps one replay ledger for every client: a request id seen before with a
    // different body is a conflict. A second tab or browser must still be able to create.
    const ledger = new Map<string, string>();
    const createIds: string[] = [];
    const handler = async (_path: string, init: RequestInit) => {
      const text = String(init.body);
      const body = JSON.parse(text) as Record<string, unknown>;
      const requestId = String(body.request_id);
      if (body.action === "create_workspace_from_context")
        createIds.push(requestId);
      const previous = ledger.get(requestId);
      if (previous !== undefined && previous !== text)
        return { ok: false, status: 409, json: async () => ({}) };
      ledger.set(requestId, text);
      return {
        ok: true,
        status: 201,
        json: async () => productionProjectionWire(),
      };
    };

    const first = await loadEntry(handler);
    act(() => first.fixture.dispatchProjection("17"));
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    act(() => first.fixture.registeredTab().destroy());
    cleanup();
    document.body.replaceChildren();
    window.sessionStorage.clear();

    const second = await loadEntry(handler);
    act(() =>
      second.fixture.dispatchProjection("17", {
        promptId: "prompt-second-session",
        workspaceId: `ws_${"e".repeat(40)}`,
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });

    expect(createIds).toHaveLength(2);
    expect(new Set(createIds).size).toBe(2);
  });

  it("quarantines a bounced Production entry and requires explicit recovery", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    let resolveCreate:
      | ((value: {
          ok: boolean;
          status: number;
          json(): Promise<unknown>;
        }) => void)
      | undefined;
    const pending = new Promise<{
      ok: boolean;
      status: number;
      json(): Promise<unknown>;
    }>((resolve) => {
      resolveCreate = resolve;
    });
    const { fixture } = await loadEntry(async (_path, init) => {
      bodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return pending;
    });
    act(() => fixture.dispatchProjection("17"));

    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByText("Production action in progress.");
    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    expect(bodies).toHaveLength(1);

    resolveCreate?.({
      ok: true,
      status: 201,
      json: async () => productionProjectionWire(),
    });
    expect(screen.queryByRole("region", { name: "Segment" })).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Retry Production setup" }),
    );
    await screen.findByRole("region", { name: "Segment" });
    expect(bodies).toHaveLength(2);
    expect(bodies[0]?.request_id).toBe(bodies[1]?.request_id);
  });

  it("settles a Context-drifted ensure as closed recovery without installing it", async () => {
    let resolveCreate:
      | ((value: {
          ok: boolean;
          status: number;
          json(): Promise<unknown>;
        }) => void)
      | undefined;
    const pending = new Promise<{
      ok: boolean;
      status: number;
      json(): Promise<unknown>;
    }>((resolve) => {
      resolveCreate = resolve;
    });
    const { fixture } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (body.action === "create_workspace_from_context") return pending;
      return { ok: false, status: 503, json: async () => ({}) };
    });
    act(() => fixture.dispatchProjection("17"));

    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByText("Production action in progress.");
    act(() =>
      fixture.dispatchProjection("17", {
        promptId: "prompt-drift",
        workspaceId: `ws_${"d".repeat(40)}`,
      }),
    );

    resolveCreate?.({
      ok: true,
      status: 201,
      json: async () => productionProjectionWire(),
    });
    await screen.findByRole("alert");

    expect(
      screen.getByText("Production workspace is unavailable."),
    ).not.toBeNull();
    expect(screen.queryByText("Production action in progress.")).toBeNull();
    expect(screen.queryByRole("region", { name: "Segment" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Retry Production setup" }),
    ).not.toBeNull();
    expect(
      window.sessionStorage.getItem(
        "h3.context.production.workspace_handle.v1",
      ),
    ).toBeNull();
    expect(fixture.productionFetchBodies()).toHaveLength(1);
  });

  it("shows a closed failure without hidden retry and permits explicit recovery", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const { fixture } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      bodies.push(body);
      if (bodies.length === 1) throw new TypeError("synthetic network failure");
      return {
        ok: true,
        status: 201,
        json: async () => productionProjectionWire(),
      };
    });
    act(() => fixture.dispatchProjection("17"));

    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    expect(bodies).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: /Retry Production/i }));
    await screen.findByRole("region", { name: "Segment" });
    expect(bodies).toHaveLength(2);
    expect(bodies[0]?.request_id).toBe(bodies[1]?.request_id);
  });
});

afterEach(async () => {
  const { managedJournal } = await import("../src/state/managedJournal");
  managedJournal.reset();
  appModeStartHarness.start = undefined;
  appModeStartHarness.workflow = undefined;
  appModeStartHarness.prepare = undefined;
  appModeAdmissionHarness.answers = [];
  appModeAdmissionHarness.calls = 0;
  appModeConnectHarness.candidates = {
    tier: "unavailable",
    anchors: [],
  };
  cleanup();
  document.body.replaceChildren();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("entry App Mode terminal recovery", () => {
  it("records the closed statechart transition and effect stream", async () => {
    const { container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );
    const { managedJournal } = await import("../src/state/managedJournal");
    await waitFor(() =>
      expect(managedJournal.snapshot()).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            kind: "transition",
            event: "START",
            from: "idle",
            to: "census",
          }),
          expect.objectContaining({
            kind: "effect",
            name: "queue",
            owner: "QueueSubmissionTransaction",
          }),
        ]),
      ),
    );
    expect(JSON.stringify(managedJournal.snapshot())).not.toContain("payload");
  });

  it("terminalizes exact failures with local copy and both recovery paths", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() =>
      fixture.dispatchExecutionTerminal("error", "prompt-1", {
        exception_message: "private host diagnostic must not render",
        traceback: ["private traceback"],
      }),
    );

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="error"]'),
      ).not.toBeNull(),
    );
    expect(
      screen.getByText("The H3 execution failed on the host."),
    ).toBeTruthy();
    expect(screen.queryByText(/private host diagnostic/i)).toBeNull();
    expect(
      screen.getByRole("button", { name: "Retry H3 App Mode" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /native nodes/i })).toBeTruthy();
  });

  it("ignores foreign terminals and handles exact interruption idempotently", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );
    act(() => fixture.dispatchExecutionTerminal("error", "foreign-prompt"));
    expect(
      container.querySelector('[data-shell-status="working"]'),
    ).not.toBeNull();

    act(() => {
      fixture.dispatchExecutionTerminal("interrupted", "prompt-1");
      fixture.dispatchExecutionTerminal("interrupted", "prompt-1");
    });
    await screen.findByText("The H3 execution was interrupted on the host.");
    expect(
      container.querySelector('[data-shell-status="error"]'),
    ).not.toBeNull();
  });

  it("requires an accepted projection when exact execution succeeds", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );
    act(() => fixture.dispatchExecutionTerminal("success", "prompt-1"));
    await screen.findByText(
      "The H3 execution finished, but its verified Context result was not received.",
    );
    expect(container.querySelector('[data-shell-status="working"]')).toBeNull();
  });

  it("keeps an accepted projection when exact success follows it", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    act(() => fixture.dispatchProjection("17"));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    act(() => fixture.dispatchExecutionTerminal("success", "prompt-1"));
    expect(
      container.querySelector('[data-shell-status="projected"]'),
    ).not.toBeNull();
  });

  it("keeps editing setup through exact success and reuses the accepted graph", async () => {
    let fixture: EntryHostFixture | undefined;
    const calls: Array<{
      inputs: AppModeInputs;
      options: AppModeStartOptions;
    }> = [];
    appModeStartHarness.start = async (inputs, options = {}) => {
      calls.push({ inputs, options });
      return {
        queueResult: acceptedQueueReceipt(`prompt-${calls.length}`),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(fixture!.app.graph.serialize()),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: `prompt-${calls.length}`,
        route: options.useExisting === true ? "existing" : "replace",
      };
    };
    const loaded = await loadEntry(
      undefined,
      compatibleExistingGraph() as Record<string, unknown>,
    );
    fixture = loaded.fixture;

    await clickReadyAppModeStart();
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.options).toMatchObject({ useExisting: true });
    act(() => fixture!.dispatchProjection("17", { promptId: "prompt-1" }));
    await waitFor(() =>
      expect(
        loaded.container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );

    const effectsBeforeEdit = fixture.sideEffectCounts();
    fireEvent.click(
      screen.getByRole("button", { name: "Edit App Mode setup" }),
    );
    await waitFor(() =>
      expect(
        loaded.container.querySelector('[data-shell-status="editing_setup"]'),
      ).not.toBeNull(),
    );
    act(() => fixture!.dispatchExecutionTerminal("success", "foreign-prompt"));
    expect(
      loaded.container.querySelector('[data-shell-status="editing_setup"]'),
    ).not.toBeNull();
    act(() => fixture!.dispatchExecutionTerminal("success", "prompt-1"));
    expect(
      loaded.container.querySelector('[data-shell-status="editing_setup"]'),
    ).not.toBeNull();
    expect(
      loaded.container.querySelector('[data-shell-status="error"]'),
    ).toBeNull();
    expect(fixture.sideEffectCounts()).toEqual(effectsBeforeEdit);

    await clickReadyAppModeStart();
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]?.options).toMatchObject({ useExisting: true });
    expect(calls[1]?.options.replaceExisting).not.toBe(true);
    expect(calls[1]?.options.connectExisting).toBeUndefined();
    expect(fixture.sideEffectCounts()).toEqual(effectsBeforeEdit);
  });

  it.each([
    ["error", "The H3 execution failed on the host."],
    ["interrupted", "The H3 execution was interrupted on the host."],
  ] as const)(
    "keeps exact %s terminal authority while editing setup",
    async (kind, message) => {
      let fixture: EntryHostFixture | undefined;
      appModeStartHarness.start = async (_inputs, options = {}) => ({
        queueResult: acceptedQueueReceipt("prompt-edit-terminal"),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(fixture!.app.graph.serialize()),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: "prompt-edit-terminal",
        route: options.useExisting === true ? "existing" : "replace",
      });
      const loaded = await loadEntry(
        undefined,
        compatibleExistingGraph() as Record<string, unknown>,
      );
      fixture = loaded.fixture;

      await clickReadyAppModeStart();
      act(() =>
        fixture!.dispatchProjection("17", {
          promptId: "prompt-edit-terminal",
        }),
      );
      await waitFor(() =>
        expect(
          loaded.container.querySelector('[data-shell-status="projected"]'),
        ).not.toBeNull(),
      );
      fireEvent.click(
        screen.getByRole("button", { name: "Edit App Mode setup" }),
      );
      await waitFor(() =>
        expect(
          loaded.container.querySelector('[data-shell-status="editing_setup"]'),
        ).not.toBeNull(),
      );

      act(() =>
        fixture!.dispatchExecutionTerminal(kind, "prompt-edit-terminal"),
      );
      await screen.findByText(message);
      expect(
        loaded.container.querySelector('[data-shell-status="error"]'),
      ).not.toBeNull();
    },
  );

  it("replays an exact terminal that wins the queue-response race", async () => {
    const deferred = deferredAppModeStart();
    appModeStartHarness.start = () => deferred.promise;
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    act(() => fixture.dispatchExecutionTerminal("error", "prompt-race"));
    await act(async () => {
      deferred.resolve({
        queueResult: acceptedQueueReceipt("prompt-race"),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(fixture.app.graph.serialize()),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: "prompt-race",
        route: "replace",
      });
      await deferred.promise;
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="error"]'),
      ).not.toBeNull(),
    );
    expect(
      screen.getByText("The H3 execution failed on the host."),
    ).toBeTruthy();
  });

  it("accepts a raced projection before replaying exact success", async () => {
    const deferred = deferredAppModeStart();
    appModeStartHarness.start = () => deferred.promise;
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    act(() => {
      fixture.dispatchProjection("17", { promptId: "prompt-race" });
      fixture.dispatchExecutionTerminal("success", "prompt-race");
    });
    await act(async () => {
      deferred.resolve({
        queueResult: acceptedQueueReceipt("prompt-race"),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(fixture.app.graph.serialize()),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: "prompt-race",
        route: "replace",
      });
      await deferred.promise;
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
  });

  it("quarantines the old prompt across Retry and correlates the new run", async () => {
    let fixture: EntryHostFixture | undefined;
    let calls = 0;
    appModeStartHarness.start = async () => {
      calls += 1;
      return {
        queueResult: acceptedQueueReceipt(`prompt-${calls}`),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(fixture!.app.graph.serialize()),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: `prompt-${calls}`,
        route: "replace",
      };
    };
    const loaded = await loadEntry();
    fixture = loaded.fixture;
    await clickReadyAppModeStart();
    act(() => fixture!.dispatchExecutionTerminal("error", "prompt-1"));
    fireEvent.click(
      await screen.findByRole("button", { name: "Retry H3 App Mode" }),
    );
    await waitFor(() => expect(calls).toBe(2));

    act(() => fixture!.dispatchExecutionTerminal("success", "prompt-1"));
    expect(
      loaded.container.querySelector('[data-shell-status="working"]'),
    ).not.toBeNull();
    act(() => fixture!.dispatchExecutionTerminal("error", "prompt-2"));
    await screen.findByText("The H3 execution failed on the host.");
    expect(calls).toBe(2);
  });

  it("reinspects and explicitly queues a compatible canvas after native recovery", async () => {
    let fixture: EntryHostFixture | undefined;
    const calls: Array<{
      inputs: AppModeInputs;
      options: AppModeStartOptions;
    }> = [];
    appModeStartHarness.start = async (inputs, options = {}) => {
      calls.push({ inputs, options });
      return {
        queueResult: acceptedQueueReceipt(`prompt-${calls.length}`),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(fixture!.app.graph.serialize()),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: `prompt-${calls.length}`,
        route: options.useExisting === true ? "existing" : "replace",
      };
    };
    const loaded = await loadEntry();
    fixture = loaded.fixture;
    const { container } = loaded;
    await clickReadyAppModeStart();
    const existingGraph = compatibleExistingGraph();
    expect(
      inspectVisibleH3Graph(existingGraph, H3_SHELL_MANIFEST),
    ).toMatchObject({ status: "ready", existingGraphCompatible: true });
    fixture.setGraph(existingGraph);
    act(() => fixture.dispatchExecutionTerminal("error", "prompt-1"));
    await screen.findByText("The H3 execution failed on the host.");
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: /native nodes/i }));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-reason="native_preference"]'),
      ).not.toBeNull(),
    );
    act(() => fixture.dispatchExecutionTerminal("error", "prompt-1"));
    expect(
      container.querySelector('[data-shell-reason="native_preference"]'),
    ).not.toBeNull();
    const intent = await screen.findByRole("textbox", { name: "Intent" });
    const duration = screen.getByRole("spinbutton", {
      name: "Clip duration (seconds)",
    });
    fireEvent.change(intent, {
      target: { value: "A synthetic existing-canvas recovery request." },
    });
    fireEvent.change(duration, { target: { value: "8" } });
    await screen.findByText("Delivers 8 s (192 frames).");
    const queueCurrent = await screen.findByRole("button", {
      name: "Apply and queue current H3 graph",
    });
    expect(calls).toHaveLength(1);
    fireEvent.click(queueCurrent);
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]?.inputs).toMatchObject({
      task_mode: "t2va",
      user_intent: "A synthetic existing-canvas recovery request.",
      duration_milliseconds: 8000,
      frame_count: 192,
    });
    expect(calls[1]?.options).toMatchObject({ useExisting: true });
    expect(calls[1]?.options.replaceExisting).not.toBe(true);
    expect(calls[1]?.options.connectExisting).toBeUndefined();
  });

  it("keeps a dirty canvas through the recovery refresh without side effects", async () => {
    const { fixture, container } = await loadEntry();
    const extension = fixture.registeredExtension() as ReturnType<
      EntryHostFixture["registeredExtension"]
    > & { loadedGraphNode?: () => void };
    fixture.setGraph({ nodes: [{ id: 1, type: "Foreign.Node" }] });
    act(() => extension.loadedGraphNode?.());
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-reason="dirty_graph"]'),
      ).not.toBeNull(),
    );
    const beforeGraph = fixture.app.graph.serialize();
    const beforeEffects = fixture.sideEffectCounts();
    const keep = container.querySelector<HTMLButtonElement>(
      '[data-h3-focus-key="app-keep-canvas"]',
    );
    expect(keep).not.toBeNull();
    fireEvent.click(keep!);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(
      container.querySelector('[data-shell-reason="native_preference"]'),
    ).not.toBeNull();
    expect(keep?.textContent).toBe("Keep canvas and exit H3 App Mode");
    expect(fixture.app.graph.serialize()).toEqual(beforeGraph);
    expect(fixture.sideEffectCounts()).toEqual(beforeEffects);

    act(() => extension.loadedGraphNode?.());
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-reason="dirty_graph"]'),
      ).not.toBeNull(),
    );
  });

  it("clears a deferred terminal when a graph configure supersedes the run", async () => {
    const deferred = deferredAppModeStart();
    appModeStartHarness.start = () => deferred.promise;
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    act(() => {
      fixture.dispatchExecutionTerminal("error", "prompt-old");
      fixture.registeredExtension().beforeConfigureGraph?.();
    });
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-reason="cancelled"]'),
      ).not.toBeNull(),
    );
    await act(async () => {
      deferred.resolve({
        queueResult: acceptedQueueReceipt("prompt-old"),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(fixture.app.graph.serialize()),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: "prompt-old",
        route: "replace",
      });
      await deferred.promise;
      await Promise.resolve();
    });
    expect(
      container.querySelector('[data-shell-reason="cancelled"]'),
    ).not.toBeNull();
    expect(container.querySelector('[data-shell-status="error"]')).toBeNull();
  });

  it("bounds pre-receipt terminal retention and still handles the active ID", async () => {
    const deferred = deferredAppModeStart();
    appModeStartHarness.start = () => deferred.promise;
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    act(() => {
      fixture.dispatchExecutionTerminal("error", "prompt-evicted");
      for (let index = 0; index < 8; index += 1)
        fixture.dispatchExecutionTerminal("error", `foreign-${index}`);
    });
    await act(async () => {
      deferred.resolve({
        queueResult: acceptedQueueReceipt("prompt-evicted"),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(fixture.app.graph.serialize()),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: "prompt-evicted",
        route: "replace",
      });
      await deferred.promise;
      await Promise.resolve();
    });
    expect(
      container.querySelector('[data-shell-status="working"]'),
    ).not.toBeNull();
    act(() =>
      fixture.dispatchExecutionTerminal("interrupted", "prompt-evicted"),
    );
    await screen.findByText("The H3 execution was interrupted on the host.");
  });
});

describe("entry Production media-preview lifecycle", () => {
  it("opens one bounded Blob session and revokes it on explicit close", async () => {
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:entry-preview");
    const revokeObjectURL = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    const { fixture } = await loadEntry(async (path) =>
      path.endsWith("/media-preview")
        ? previewResponse()
        : {
            ok: true,
            status: 201,
            json: async () => productionPreviewProjectionWire(),
          },
    );
    act(() => fixture.dispatchProjection("17"));
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    fireEvent.click(
      await screen.findByRole("button", { name: "Preview aggregate output" }),
    );
    await screen.findByLabelText("Aggregate output preview");
    expect(createObjectURL).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "Close preview" }));
    expect(screen.queryByLabelText("Aggregate output preview")).toBeNull();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:entry-preview");
  });

  it("admits an exact clip and revokes it before aggregate replacement", async () => {
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValueOnce("blob:entry-clip")
      .mockReturnValueOnce("blob:entry-aggregate");
    const revokeObjectURL = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    const requestedHandles: string[] = [];
    const { fixture } = await loadEntry(async (path, init) => {
      if (path.endsWith("/media-preview")) {
        const body = JSON.parse(String(init.body)) as { output_handle: string };
        requestedHandles.push(body.output_handle);
        return previewResponse();
      }
      return {
        ok: true,
        status: 201,
        json: async () => productionClipPreviewProjectionWire(),
      };
    });
    act(() => fixture.dispatchProjection("17"));
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });

    fireEvent.click(
      (await screen.findAllByRole("button", { name: "Preview segment 1" }))[0]!,
    );
    await screen.findByLabelText("Segment 1 preview");
    expect(createObjectURL).toHaveBeenCalledTimes(1);

    fireEvent.click(
      screen.getByRole("button", { name: "Preview aggregate output" }),
    );
    await screen.findByLabelText("Aggregate output preview");
    expect(createObjectURL).toHaveBeenCalledTimes(2);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:entry-clip");
    expect(revokeObjectURL.mock.invocationCallOrder[0]).toBeLessThan(
      createObjectURL.mock.invocationCallOrder[1]!,
    );
    expect(requestedHandles).toEqual([
      `out_${"d".repeat(40)}`,
      `out_${"c".repeat(40)}`,
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Close preview" }));
    expect(revokeObjectURL).toHaveBeenLastCalledWith("blob:entry-aggregate");
  });

  it("keeps exact clip preview capability across a selection journey", async () => {
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValueOnce("blob:entry-clip-1")
      .mockReturnValueOnce("blob:entry-clip-2");
    const revokeObjectURL = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    const requestedHandles: string[] = [];
    const productionActions: string[] = [];
    const { fixture } = await loadEntry(async (path, init) => {
      if (path.endsWith("/media-preview")) {
        const body = JSON.parse(String(init.body)) as { output_handle: string };
        requestedHandles.push(body.output_handle);
        return previewResponse();
      }
      const body = JSON.parse(String(init.body)) as {
        action: string;
        payload?: { segment_ids?: string[] };
      };
      productionActions.push(body.action);
      if (body.action === "create_workspace_from_context")
        return {
          ok: true,
          status: 201,
          json: async () => productionClipJourneyProjectionWire(1, "segment_1"),
        };
      expect(body).toMatchObject({
        action: "set_selection",
        payload: { segment_ids: ["segment_2"] },
      });
      return {
        ok: true,
        status: 200,
        json: async () => productionClipJourneyProjectionWire(2, "segment_2"),
      };
    });
    act(() => fixture.dispatchProjection("17"));
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });

    fireEvent.click(
      (await screen.findAllByRole("button", { name: "Preview segment 1" }))[0]!,
    );
    await screen.findByLabelText("Segment 1 preview");
    fireEvent.click(screen.getByRole("button", { name: "Select segment 2" }));
    await screen.findByLabelText("revision 2");
    expect(
      await screen.findAllByRole("button", { name: "Preview segment 2" }),
    ).toHaveLength(2);
    fireEvent.click(
      (await screen.findAllByRole("button", { name: "Preview segment 2" }))[0]!,
    );
    await screen.findByLabelText("Segment 2 preview");

    expect(productionActions).toEqual([
      "create_workspace_from_context",
      "set_selection",
    ]);
    expect(requestedHandles).toEqual([
      `out_${"d".repeat(40)}`,
      `out_${"e".repeat(40)}`,
    ]);
    expect(createObjectURL).toHaveBeenCalledTimes(2);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:entry-clip-1");
    expect(revokeObjectURL.mock.invocationCallOrder[0]).toBeLessThan(
      createObjectURL.mock.invocationCallOrder[1]!,
    );
    fireEvent.click(screen.getByRole("button", { name: "Close preview" }));
    expect(revokeObjectURL).toHaveBeenLastCalledWith("blob:entry-clip-2");
  });

  it("quarantines a late preview after navigation and tab unmount", async () => {
    let resolvePreview: ((response: Response) => void) | undefined;
    const pending = new Promise<Response>((resolve) => {
      resolvePreview = resolve;
    });
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:late-preview");
    const { fixture, container } = await loadEntry(async (path) =>
      path.endsWith("/media-preview")
        ? pending
        : {
            ok: true,
            status: 201,
            json: async () => productionPreviewProjectionWire(),
          },
    );
    act(() => fixture.dispatchProjection("17"));
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    fireEvent.click(
      await screen.findByRole("button", { name: "Preview aggregate output" }),
    );
    await screen.findByText("Preparing preview…");
    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    resolvePreview?.(previewResponse());
    await act(async () => {
      await pending;
      await Promise.resolve();
    });
    act(() => tab.render(container));
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    expect(screen.queryByLabelText("Aggregate output preview")).toBeNull();
    expect(createObjectURL).not.toHaveBeenCalled();
  });
});

describe("entry managed App Mode Production lifecycle", () => {
  it.each([["mutate_in_place"], ["forward_copy"]] as const)(
    "survives a patched queue wrapper that uses %s on the only managed prompt",
    async (wrapperMode) => {
      let initialProduction!: Record<string, unknown>;
      let managedFixture!: ReturnType<typeof managedEntryFixture>;
      const { fixture, container } = await loadEntry(async (path, init) => {
        const body = JSON.parse(String(init.body)) as { action?: string };
        if (path === "/h3-context/v1/production/action")
          return {
            ok: true,
            status: 201,
            json: async () => initialProduction,
          };
        if (path === "/h3-context/v1/generation/coordinator") {
          const submitted = managedSequenceStateWire(
            managedFixture.planned,
            "submitted",
          );
          if (body.action === "prepare_managed_run")
            return {
              ok: true,
              status: 200,
              json: async () =>
                managedCoordinatorResponse(
                  "prepared",
                  managedFixture.planned,
                  managedProductionStateWire(
                    managedFixture.planned,
                    "planned",
                    managedFixture.planned,
                  ),
                ),
            };
          if (body.action === "submit_managed_run")
            return {
              ok: true,
              status: 200,
              json: async () =>
                managedCoordinatorResponse(
                  "submitted",
                  submitted,
                  managedProductionStateWire(
                    submitted,
                    "submitted",
                    managedFixture.planned,
                  ),
                ),
            };
        }
        return { ok: false, status: 503, json: async () => ({}) };
      });
      managedFixture = managedEntryFixture(
        appModeFingerprint(fixture.app.graph.serialize()),
        fixture.workflowState().activeWorkflow,
        fixture.app.graph.serialize(),
      );
      initialProduction = managedFixture.production;
      fixture.setQueuePromptIds(["prompt.model.1"]);
      fixture.setQueuePromptWrapperMode(wrapperMode);
      let queueCountBeforeModel = -1;
      let accepted = false;
      appModeStartHarness.start = async (_inputs, options = {}) => {
        const authority = await options.prepareManaged!(
          managedFixture.preparation,
        );
        authority.bindCanvasIdentity?.(
          managedFixture.preparation.managedIdentity,
        );
        queueCountBeforeModel = fixture.sideEffectCounts().queues;
        authority.onQueueSubmitted();
        const queued = await fixture.api.queuePrompt(-1, {
          output: {
            "17": {
              class_type: "comfyui_h3_context.H3Context.ProductShell",
              inputs: {},
            },
            "6": { class_type: "MiniMaxH3ImageToVideo", inputs: {} },
            "43": { class_type: "SaveVideo", inputs: {} },
          },
          workflow: structuredClone(
            managedFixture.preparation.bootstrap.workflow,
          ),
        });
        const queueResult = acceptedQueueReceipt(
          queued.prompt_id,
          queued.number,
        );
        const result: AppModeStartResult = {
          queueResult,
          ...OWNED_RESULT_IDENTITY,
          graphFingerprint:
            managedFixture.preparation.observation.graph_fingerprint,
          compiledPromptFingerprint:
            managedFixture.planned.eligible_commands[0]!
              .compiled_prompt_fingerprint,
          queuePromptId: queueResult.prompt_id,
          route: "existing",
        };
        await authority.onQueueAccepted(result);
        accepted = true;
        return result;
      };

      fireEvent.change(
        screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
        { target: { value: "8" } },
      );
      await screen.findByText("Delivers 8 s (192 frames).");
      await clickReadyAppModeStart();
      await waitFor(() => expect(fixture.sideEffectCounts().queues).toBe(1));
      act(() =>
        fixture.dispatchProjection("17", {
          promptId: "prompt.model.1",
          workspaceId: managedFixture.workspaceId,
          requestedSeconds: 8,
          taskMode: "t2va",
        }),
      );
      await waitFor(() => expect(accepted).toBe(true));

      const callerWorkflow = managedFixture.preparation.bootstrap
        .workflow as Record<string, unknown>;
      const forwarded = fixture.queuedPrompts()[0] as {
        output: Record<string, { class_type?: string }>;
        workflow: Record<string, unknown>;
      };
      expect(forwarded.workflow).toMatchObject({
        nodes: [],
        links: [],
        widget_idx_map: {},
        seed_widgets: {},
      });
      expect(forwarded.workflow).not.toBe(callerWorkflow);
      expect(Object.hasOwn(callerWorkflow, "widget_idx_map")).toBe(false);
      expect(Object.hasOwn(callerWorkflow, "seed_widgets")).toBe(false);
      expect(
        Object.values(forwarded.output)
          .map((node) => node.class_type)
          .sort(),
      ).toEqual([
        "MiniMaxH3ImageToVideo",
        "SaveVideo",
        "comfyui_h3_context.H3Context.ProductShell",
      ]);
      expect(queueCountBeforeModel).toBe(0);
      expect(fixture.sideEffectCounts().queues).toBe(1);
      expect(container.querySelector('[data-shell-status="error"]')).toBeNull();
      fireEvent.click(
        await screen.findByRole("button", { name: "Production" }),
      );
      expect(
        await screen.findByText(
          "No intent proposal was supplied for the selected segments.",
        ),
      ).toBeDefined();
      expect(
        screen
          .getByText(
            "No intent proposal was supplied for the selected segments.",
          )
          .getAttribute("role"),
      ).toBe("status");
      expect(screen.queryByText(/Proposal review \d+ of \d+/)).toBeNull();
    },
  );

  it.each([
    ["before_prompt_bind", "success"],
    ["after_prompt_bind", "success"],
    ["during_prepare", "success"],
    ["during_prepare", "error"],
    ["during_prepare", "interrupted"],
  ] as const)(
    "transfers an exact terminal arriving %s with kind %s",
    async (terminalTiming, terminalKind) => {
      const coordinatorActions: string[] = [];
      const coordinatorBodies: Array<{
        action?: string;
        payload?: Record<string, unknown>;
      }> = [];
      const requestedPreviewHandles: string[] = [];
      vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:managed-segment");
      let planned!: EntryGenerationSequenceWire;
      let submitted!: EntryGenerationSequenceWire;
      let running!: EntryGenerationSequenceWire;
      let succeeded!: EntryGenerationSequenceWire;
      let initialProduction!: Record<string, unknown>;
      let releasePreparation!: () => void;
      const preparationGate = new Promise<void>((resolve) => {
        releasePreparation = resolve;
      });
      let actionsDuringPreparation: string[] = [];
      const { fixture, container } = await loadEntry(async (path, init) => {
        const body = JSON.parse(String(init.body)) as {
          action?: string;
          payload?: Record<string, unknown>;
          output_handle?: string;
        };
        if (path.endsWith("/media-preview")) {
          requestedPreviewHandles.push(String(body.output_handle));
          return previewResponse();
        }
        if (path === "/h3-context/v1/production/action")
          return {
            ok: true,
            status: 201,
            json: async () => initialProduction,
          };
        if (path === "/h3-context/v1/generation/coordinator") {
          coordinatorActions.push(String(body.action));
          coordinatorBodies.push(body);
          if (body.action === "prepare_managed_run") await preparationGate;
          if (body.action === "close_managed_run" && terminalKind !== "success")
            return {
              ok: true,
              status: 200,
              json: async () =>
                managedCoordinatorResponse(
                  terminalKind === "interrupted" ? "interrupted" : "failed",
                  running,
                  managedProductionStateWire(running, "running", planned),
                ),
            };
          const fixtureByAction = {
            prepare_managed_run: managedCoordinatorResponse(
              "prepared",
              planned,
              managedProductionStateWire(planned, "planned", planned),
            ),
            submit_managed_run: managedCoordinatorResponse(
              "submitted",
              submitted,
              managedProductionStateWire(submitted, "submitted", planned),
            ),
            close_managed_run: managedCoordinatorResponse(
              "succeeded",
              succeeded,
              managedProductionStateWire(succeeded, "succeeded", planned),
            ),
          } as const;
          const payload =
            fixtureByAction[body.action as keyof typeof fixtureByAction];
          if (payload === undefined)
            return { ok: false, status: 422, json: async () => ({}) };
          return { ok: true, status: 200, json: async () => payload };
        }
        return { ok: false, status: 503, json: async () => ({}) };
      });

      const noisyHost = createNoisyHostExtension();
      const noisyGraph = fixture.app.graph.serialize() as Json;
      const noisyNodes = Array.isArray(noisyGraph.nodes)
        ? (noisyGraph.nodes as Json[])
        : [];
      noisyNodes.push({
        id: 99,
        type: "ForeignSeed",
        pos: [50, 60],
        properties: {},
        widgets_values: [41],
      });
      noisyGraph.nodes = noisyNodes;
      for (const node of noisyNodes) noisyHost.nodeCreated(node);
      noisyHost.afterConfigureGraph(noisyGraph);
      fixture.setGraph(noisyGraph);
      const graphFingerprint = appModeFingerprint(
        fixture.app.graph.serialize(),
      );
      let driftedGraphFingerprint = graphFingerprint;
      const managedFixture = managedEntryFixture(
        graphFingerprint,
        fixture.workflowState().activeWorkflow,
        fixture.app.graph.serialize(),
      );
      const managedPreparation = managedFixture.preparation;
      planned = managedFixture.planned;
      submitted = managedSequenceStateWire(planned, "submitted");
      running = managedSequenceStateWire(planned, "running");
      succeeded = managedSequenceStateWire(planned, "succeeded");
      initialProduction = managedFixture.production;
      expect(() => decodeGenerationSequenceProjection(planned)).not.toThrow();
      expect(() =>
        decodeProductionWorkbenchProjection(
          managedProductionStateWire(planned, "planned"),
        ),
      ).not.toThrow();
      fixture.setQueuePromptIds(["prompt.model.1"]);

      let preparationEntered = false;
      let managedCallbackAvailable = false;
      let managedPreparationFailure = "";
      appModeStartHarness.start = async (_inputs, options = {}) => {
        expect(options.useExisting).toBe(true);
        preparationEntered = true;
        managedCallbackAvailable = typeof options.prepareManaged === "function";
        let authority;
        try {
          authority = await options.prepareManaged!(managedPreparation);
        } catch (error) {
          managedPreparationFailure =
            error instanceof Error
              ? `${error.name}: ${error.message}`
              : String(error);
          throw error;
        }
        expect(coordinatorActions).toEqual([]);
        const driftedGraph = fixture.app.graph.serialize() as Json;
        noisyHost.afterConfigureGraph(driftedGraph);
        for (const node of driftedGraph.nodes as Json[])
          noisyHost.nodeCreated(node);
        fixture.setGraph(driftedGraph);
        driftedGraphFingerprint = appModeFingerprint(
          fixture.app.graph.serialize(),
        );
        authority.bindCanvasIdentity?.(managedPreparation.managedIdentity);
        authority.onQueueSubmitted();
        const queued = await fixture.api.queuePrompt(-1, {
          output: {
            "17": {
              class_type: "comfyui_h3_context.H3Context.ProductShell",
              inputs: {},
            },
            "6": { class_type: "MiniMaxH3ImageToVideo", inputs: {} },
            "43": { class_type: "SaveVideo", inputs: {} },
          },
          workflow: structuredClone(
            managedFixture.preparation.bootstrap.workflow,
          ),
        });
        const queueResult = acceptedQueueReceipt(
          queued.prompt_id,
          queued.number,
        );
        const result: AppModeStartResult = {
          queueResult,
          ...OWNED_RESULT_IDENTITY,
          graphFingerprint,
          compiledPromptFingerprint:
            planned.eligible_commands[0]!.compiled_prompt_fingerprint,
          queuePromptId: queueResult.prompt_id,
          route: "existing",
        };
        if (terminalTiming === "before_prompt_bind") {
          fixture.dispatchExecutionTerminal(terminalKind, "prompt.model.1");
          fixture.dispatchExecutionTerminal("error", "prompt.foreign.1");
          fixture.dispatchSaveVideoArtifact("prompt.model.1", "43");
          fixture.dispatchSaveVideoArtifact("prompt.foreign.1", "43");
        }
        const acceptance = authority.onQueueAccepted(result);
        await acceptance;
        return result;
      };

      fireEvent.change(
        screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
        { target: { value: "8" } },
      );
      await screen.findByText("Delivers 8 s (192 frames).");
      await clickReadyAppModeStart();
      await waitFor(() => expect(preparationEntered).toBe(true));
      expect(managedCallbackAvailable).toBe(true);
      await waitFor(() => expect(fixture.sideEffectCounts().queues).toBe(1));
      if (terminalTiming === "after_prompt_bind") {
        const { managedJournal } = await import("../src/state/managedJournal");
        await waitFor(() =>
          expect(managedJournal.snapshot()).toEqual(
            expect.arrayContaining([
              expect.objectContaining({
                kind: "stage",
                name: "bootstrap_prompt_bound",
              }),
            ]),
          ),
        );
        fixture.dispatchExecutionTerminal(terminalKind, "prompt.model.1");
        fixture.dispatchExecutionTerminal("error", "prompt.foreign.1");
        fixture.dispatchSaveVideoArtifact("prompt.model.1", "43");
        fixture.dispatchSaveVideoArtifact("prompt.foreign.1", "43");
      }
      act(() =>
        fixture.dispatchProjection("17", {
          promptId: "prompt.model.1",
          workspaceId: managedFixture.workspaceId,
          requestedSeconds: 8,
          taskMode: "t2va",
          semanticProposalReview: semanticProposalHandleWire(),
        }),
      );
      await waitFor(() =>
        expect(coordinatorActions).toEqual(["prepare_managed_run"]),
      );
      if (terminalTiming === "during_prepare") {
        // The waiter has resolved, but managed ownership is not installed until prepare returns.
        fixture.dispatchExecutionTerminal(terminalKind, "prompt.model.1");
        fixture.dispatchExecutionTerminal("error", "prompt.foreign.1");
        fixture.dispatchSaveVideoArtifact("prompt.model.1", "43");
        fixture.dispatchSaveVideoArtifact("prompt.foreign.1", "43");
      }
      actionsDuringPreparation = [...coordinatorActions];
      releasePreparation();
      await waitFor(() =>
        expect(
          container.querySelector('[data-shell-status="projected"]') ??
            container.querySelector('[data-shell-status="error"]'),
        ).not.toBeNull(),
      );
      if (terminalKind !== "success") {
        await screen.findByText(
          terminalKind === "interrupted"
            ? "The H3 execution was interrupted on the host."
            : "The H3 execution failed on the host.",
        );
        expect(
          container.querySelector('[data-shell-status="error"]'),
        ).not.toBeNull();
        expect(
          container.querySelector('[data-h3-focus-key="error-recovery"]')
            ?.textContent,
        ).toBe("Retry H3 App Mode");
        expect(coordinatorActions).toEqual([
          "prepare_managed_run",
          "submit_managed_run",
          "close_managed_run",
        ]);
        expect(coordinatorBodies[2]).toMatchObject({
          action: "close_managed_run",
          payload: {
            queue_prompt_id: "prompt.model.1",
            kind: terminalKind,
            artifact: null,
          },
        });
        expect(actionsDuringPreparation).toEqual(["prepare_managed_run"]);
        expect(fixture.sideEffectCounts().queues).toBe(1);
        return;
      }
      expect(
        container.querySelector('[data-shell-status="error"]'),
        `managed preparation failed after coordinator actions ${JSON.stringify(coordinatorActions)}: ${managedPreparationFailure}`,
      ).toBeNull();
      expect(coordinatorActions.slice(0, 2)).toEqual([
        "prepare_managed_run",
        "submit_managed_run",
      ]);
      expect(actionsDuringPreparation).toEqual(["prepare_managed_run"]);
      expect(fixture.sideEffectCounts().queues).toBe(1);
      const queued = fixture.queuedPrompts() as Array<{
        output?: Record<string, { class_type?: string }>;
      }>;
      expect(
        Object.values(queued[0]?.output ?? {})
          .map((node) => node.class_type)
          .sort(),
      ).toEqual([
        "MiniMaxH3ImageToVideo",
        "SaveVideo",
        "comfyui_h3_context.H3Context.ProductShell",
      ]);

      await act(async () => {
        await Promise.resolve();
      });
      expect(coordinatorActions).toEqual([
        "prepare_managed_run",
        "submit_managed_run",
        "close_managed_run",
      ]);
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Production" }));
      await screen.findByText("1 of 1 segments");
      expect(
        screen.getByRole<HTMLButtonElement>("button", {
          name: /^Understand segment( 1)?$/,
        }).disabled,
      ).toBe(false);
      expect(
        screen.queryByRole("button", { name: "Generate segment 1" }),
      ).toBeNull();
      fireEvent.click(
        (
          await screen.findAllByRole("button", { name: "Preview segment 1" })
        )[0]!,
      );
      await screen.findByLabelText("Segment 1 preview");
      expect(requestedPreviewHandles).toEqual([`out_${"o".repeat(24)}`]);
      expect(coordinatorActions).toEqual([
        "prepare_managed_run",
        "submit_managed_run",
        "close_managed_run",
      ]);
      expect(coordinatorBodies[2]).toMatchObject({
        action: "close_managed_run",
        payload: {
          queue_prompt_id: "prompt.model.1",
          kind: "success",
          artifact: {
            output_node_id: "43",
            locator: {
              filename: "managed_00001_.mp4",
              subfolder: "video/h3-context",
              type: "output",
            },
          },
        },
      });
      expect(driftedGraphFingerprint).not.toBe(graphFingerprint);

      fireEvent.click(screen.getByRole("button", { name: "Context" }));
      fireEvent.click(
        screen.getByRole("button", { name: "Edit App Mode setup" }),
      );
      await waitFor(() =>
        expect(
          container.querySelector('[data-shell-status="editing_setup"]'),
        ).not.toBeNull(),
      );
      const intent = screen.getByRole<HTMLInputElement>("textbox", {
        name: "Intent",
      });
      fireEvent.change(intent, { target: { value: "Retained managed draft" } });
      const tab = fixture.registeredTab();
      act(() => tab.destroy());
      const surroundingGraph = fixture.app.graph.serialize() as Json;
      const foreignNode = (surroundingGraph.nodes as Json[]).find(
        (node) => node.id === 99,
      );
      if (foreignNode === undefined) throw new Error("missing noisy host node");
      foreignNode.pos = [500, 600];
      foreignNode.properties = { cnr_id: "synthetic-noise" };
      fixture.setGraph(surroundingGraph);
      act(() => tab.render(container));

      await waitFor(() =>
        expect(
          container.querySelector('[data-shell-status="editing_setup"]'),
        ).not.toBeNull(),
      );
      expect(
        screen.getByRole<HTMLInputElement>("textbox", { name: "Intent" }).value,
      ).toBe("Retained managed draft");
      expect(fixture.workflowState().activeWorkflow).toBe(
        managedPreparation.managedIdentity.workflowAuthority,
      );
    },
  );

  it("reconciles artifact failure and retries only output verification on the same Production run", async () => {
    const coordinatorActions: string[] = [];
    let artifactRequests = 0;
    let planned!: EntryGenerationSequenceWire;
    let submitted!: EntryGenerationSequenceWire;
    let running!: EntryGenerationSequenceWire;
    let verificationFailed!: EntryGenerationSequenceWire;
    let succeeded!: EntryGenerationSequenceWire;
    let initialProduction!: Record<string, unknown>;
    const { fixture, container } = await loadEntry(async (path, init) => {
      const body = JSON.parse(String(init.body)) as {
        action?: string;
        payload?: { artifact?: unknown };
      };
      if (path === "/h3-context/v1/production/action")
        return {
          ok: true,
          status: 201,
          json: async () => initialProduction,
        };
      if (path !== "/h3-context/v1/generation/coordinator")
        return { ok: false, status: 503, json: async () => ({}) };
      const action = String(body.action);
      coordinatorActions.push(action);
      if (action === "close_managed_run") {
        if (body.payload?.artifact === null)
          return {
            ok: true,
            status: 200,
            json: async () =>
              managedCoordinatorResponse(
                "verification_pending",
                running,
                managedProductionStateWire(running, "running", planned),
              ),
          };
        artifactRequests += 1;
        if (artifactRequests === 1)
          return {
            ok: false,
            status: 503,
            json: async () => ({
              schema: "h3.context.generation_coordinator.error.v1",
              category: "artifact_store_unavailable",
              retry_disposition: "retry_output_verification",
              same_run_authority: true,
            }),
          };
        return {
          ok: true,
          status: 200,
          json: async () =>
            managedCoordinatorResponse(
              "succeeded",
              succeeded,
              managedProductionStateWire(succeeded, "succeeded", planned),
            ),
        };
      }
      const responseByAction = {
        prepare_managed_run: managedCoordinatorResponse(
          "prepared",
          planned,
          managedProductionStateWire(planned, "planned", planned),
        ),
        submit_managed_run: managedCoordinatorResponse(
          "submitted",
          submitted,
          managedProductionStateWire(submitted, "submitted", planned),
        ),
        read_managed_run: managedCoordinatorResponse(
          "current",
          verificationFailed,
          managedProductionStateWire(
            verificationFailed,
            "output_verification_failed",
            planned,
          ),
        ),
      } as const;
      const payload = responseByAction[action as keyof typeof responseByAction];
      return payload === undefined
        ? { ok: false, status: 422, json: async () => ({}) }
        : { ok: true, status: 200, json: async () => payload };
    });
    const { managedJournal } = await import("../src/state/managedJournal");
    managedJournal.reset();

    const graphFingerprint = appModeFingerprint(fixture.app.graph.serialize());
    const managedFixture = managedEntryFixture(
      graphFingerprint,
      fixture.workflowState().activeWorkflow,
      fixture.app.graph.serialize(),
    );
    planned = managedFixture.planned;
    submitted = managedSequenceStateWire(planned, "submitted");
    running = managedSequenceStateWire(planned, "running");
    verificationFailed = managedSequenceStateWire(
      planned,
      "output_verification_failed",
    );
    succeeded = managedSequenceStateWire(planned, "succeeded");
    initialProduction = managedFixture.production;
    expect(() =>
      decodeGenerationSequenceProjection(verificationFailed),
    ).not.toThrow();
    fixture.setQueuePromptIds(["prompt.model.1"]);
    appModeStartHarness.start = async (_inputs, options = {}) => {
      const authority = await options.prepareManaged!(
        managedFixture.preparation,
      );
      authority.bindCanvasIdentity?.(
        managedFixture.preparation.managedIdentity,
      );
      authority.onQueueSubmitted();
      const queued = await fixture.api.queuePrompt(-1, {
        output: {
          "17": {
            class_type: "comfyui_h3_context.H3Context.ProductShell",
            inputs: {},
          },
          "6": { class_type: "MiniMaxH3ImageToVideo", inputs: {} },
          "43": { class_type: "SaveVideo", inputs: {} },
        },
        workflow: structuredClone(
          managedFixture.preparation.bootstrap.workflow,
        ),
      });
      const queueResult = acceptedQueueReceipt(queued.prompt_id, queued.number);
      const result: AppModeStartResult = {
        queueResult,
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint,
        compiledPromptFingerprint:
          planned.eligible_commands[0]!.compiled_prompt_fingerprint,
        queuePromptId: queueResult.prompt_id,
        route: "existing",
      };
      await authority.onQueueAccepted(result);
      return result;
    };

    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
      { target: { value: "8" } },
    );
    await screen.findByText("Delivers 8 s (192 frames).");
    await clickReadyAppModeStart();
    act(() =>
      fixture.dispatchProjection("17", {
        promptId: "prompt.model.1",
        workspaceId: managedFixture.workspaceId,
        requestedSeconds: 8,
        taskMode: "t2va",
      }),
    );
    await waitFor(() =>
      expect(
        container.querySelector('[data-app-mode-phase="generating"]'),
      ).not.toBeNull(),
    );
    expect(fixture.sideEffectCounts().queues).toBe(1);

    act(() => fixture.dispatchExecutionTerminal("success", "prompt.model.1"));
    await waitFor(() =>
      expect(
        container.querySelector('[data-app-mode-phase="verifying_output"]'),
      ).not.toBeNull(),
    );
    expect(coordinatorActions).toEqual([
      "prepare_managed_run",
      "submit_managed_run",
      "close_managed_run",
    ]);
    act(() => fixture.dispatchSaveVideoArtifact("prompt.model.1", "43"));
    await screen.findByRole("button", { name: "Retry output verification" });

    expect(coordinatorActions).toEqual([
      "prepare_managed_run",
      "submit_managed_run",
      "close_managed_run",
      "close_managed_run",
      "read_managed_run",
    ]);
    expect(screen.getByRole("alert").textContent).toContain(
      "The saved output could not be copied into the workbench's storage.",
    );
    const failedEntries = managedJournal.snapshot();
    const failedStages = failedEntries.flatMap((entry) =>
      entry.kind === "stage" ? [entry.name] : [],
    );
    expect(failedStages).toEqual(
      expect.arrayContaining([
        "bootstrap_started",
        "bootstrap_prompt_bound",
        "managed_bootstrap_returned",
        "aggregate_prepare_returned",
      ]),
    );
    expect(failedEntries).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: "error",
          name: "sequence_coordinator",
          category: "artifact_store_unavailable",
        }),
        expect.objectContaining({
          kind: "state",
          name: "error",
          code: "artifact_store_unavailable",
        }),
      ]),
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy diagnostics" }));
    const copiedDiagnostics = await screen.findByRole<HTMLTextAreaElement>(
      "textbox",
      { name: "Selectable diagnostics" },
    );
    expect(copiedDiagnostics.value).toContain("stage bootstrap_started");
    expect(copiedDiagnostics.value).toContain(
      "error sequence_coordinator category=artifact_store_unavailable",
    );
    expect(copiedDiagnostics.value).toContain(
      "state error code=artifact_store_unavailable",
    );
    expect(
      screen.queryByRole("button", { name: "Retry H3 App Mode" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    await screen.findByText("1 of 1 segments");
    expect(document.body.textContent).not.toContain(
      "Sequence authority is unavailable",
    );
    fireEvent.click(screen.getByRole("button", { name: "Context" }));

    fireEvent.click(
      screen.getByRole("button", { name: "Retry output verification" }),
    );
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    expect(coordinatorActions).toEqual([
      "prepare_managed_run",
      "submit_managed_run",
      "close_managed_run",
      "close_managed_run",
      "read_managed_run",
      "close_managed_run",
    ]);
    expect(fixture.sideEffectCounts().queues).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    await screen.findByText("1 of 1 segments");
    expect(document.body.textContent).not.toContain(
      "Sequence authority is unavailable",
    );
  });

  it.each([
    ["error", "failed"],
    ["interrupted", "interrupted"],
  ] as const)(
    "closes a %s terminal with no artifact even when the host already saved one",
    async (terminalKind, disposition) => {
      const coordinatorActions: string[] = [];
      const coordinatorBodies: Record<string, unknown>[] = [];
      let refusedInvalidArtifact = false;
      let planned!: EntryGenerationSequenceWire;
      let submitted!: EntryGenerationSequenceWire;
      let failed!: EntryGenerationSequenceWire;
      let initialProduction!: Record<string, unknown>;
      const { fixture, container } = await loadEntry(async (path, init) => {
        const body = JSON.parse(String(init.body)) as {
          action?: string;
          payload?: { kind?: unknown; artifact?: unknown };
        };
        if (path === "/h3-context/v1/production/action")
          return { ok: true, status: 201, json: async () => initialProduction };
        if (path !== "/h3-context/v1/generation/coordinator")
          return { ok: false, status: 503, json: async () => ({}) };
        const action = String(body.action);
        coordinatorActions.push(action);
        coordinatorBodies.push(body as Record<string, unknown>);
        if (action === "close_managed_run") {
          // CRITICAL: this double must reproduce the aggregate's own refusal
          // (`comfyui_sequence_coordinator.py` `_close_managed`), which rejects any artifact on a
          // non-success close with `invalid_managed_artifact`. A double that answers 200 to every
          // close lets the client ship a payload the backend refuses while every frontend suite
          // stays green -- exactly how that defect reached the supplied-host acceptance row.
          if (
            body.payload?.kind !== "success" &&
            body.payload?.artifact !== null
          ) {
            refusedInvalidArtifact = true;
            return {
              ok: false,
              status: 400,
              json: async () => ({
                schema: "h3.context.generation_coordinator.error.v1",
                category: "invalid_managed_artifact",
                retry_disposition: "none",
                same_run_authority: false,
              }),
            };
          }
          return {
            ok: true,
            status: 200,
            json: async () =>
              managedCoordinatorResponse(
                disposition,
                failed,
                managedProductionStateWire(failed, "failed", planned),
              ),
          };
        }
        const responseByAction = {
          prepare_managed_run: managedCoordinatorResponse(
            "prepared",
            planned,
            managedProductionStateWire(planned, "planned", planned),
          ),
          submit_managed_run: managedCoordinatorResponse(
            "submitted",
            submitted,
            managedProductionStateWire(submitted, "submitted", planned),
          ),
        } as const;
        const payload =
          responseByAction[action as keyof typeof responseByAction];
        return payload === undefined
          ? { ok: false, status: 422, json: async () => ({}) }
          : { ok: true, status: 200, json: async () => payload };
      });

      const graphFingerprint = appModeFingerprint(
        fixture.app.graph.serialize(),
      );
      const managedFixture = managedEntryFixture(
        graphFingerprint,
        fixture.workflowState().activeWorkflow,
        fixture.app.graph.serialize(),
      );
      planned = managedFixture.planned;
      submitted = managedSequenceStateWire(planned, "submitted");
      failed = managedSequenceStateWire(planned, "failed");
      initialProduction = managedFixture.production;
      fixture.setQueuePromptIds(["prompt.model.1"]);
      appModeStartHarness.start = async (_inputs, options = {}) => {
        const authority = await options.prepareManaged!(
          managedFixture.preparation,
        );
        authority.bindCanvasIdentity?.(
          managedFixture.preparation.managedIdentity,
        );
        authority.onQueueSubmitted();
        const queued = await fixture.api.queuePrompt(-1, {
          output: {
            "17": {
              class_type: "comfyui_h3_context.H3Context.ProductShell",
              inputs: {},
            },
            "6": { class_type: "MiniMaxH3ImageToVideo", inputs: {} },
            "43": { class_type: "SaveVideo", inputs: {} },
          },
          workflow: structuredClone(
            managedFixture.preparation.bootstrap.workflow,
          ),
        });
        const queueResult = acceptedQueueReceipt(
          queued.prompt_id,
          queued.number,
        );
        const result: AppModeStartResult = {
          queueResult,
          ...OWNED_RESULT_IDENTITY,
          graphFingerprint,
          compiledPromptFingerprint:
            planned.eligible_commands[0]!.compiled_prompt_fingerprint,
          queuePromptId: queueResult.prompt_id,
          route: "existing",
        };
        await authority.onQueueAccepted(result);
        return result;
      };

      fireEvent.change(
        screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
        { target: { value: "8" } },
      );
      await screen.findByText("Delivers 8 s (192 frames).");
      await clickReadyAppModeStart();
      act(() =>
        fixture.dispatchProjection("17", {
          promptId: "prompt.model.1",
          workspaceId: managedFixture.workspaceId,
          requestedSeconds: 8,
          taskMode: "t2va",
        }),
      );
      await waitFor(() =>
        expect(
          container.querySelector('[data-app-mode-phase="generating"]'),
        ).not.toBeNull(),
      );

      // The host saves an output and only then fails or is interrupted: a user-built graph is free
      // to place further nodes after its save, so a buffered artifact and a non-success terminal
      // genuinely coexist.
      act(() => fixture.dispatchSaveVideoArtifact("prompt.model.1", "43"));
      act(() =>
        fixture.dispatchExecutionTerminal(terminalKind, "prompt.model.1"),
      );
      await waitFor(() =>
        expect(coordinatorActions).toContain("close_managed_run"),
      );
      await act(async () => {
        await Promise.resolve();
      });

      expect(coordinatorActions).toEqual([
        "prepare_managed_run",
        "submit_managed_run",
        "close_managed_run",
      ]);
      expect(coordinatorBodies[2]).toMatchObject({
        action: "close_managed_run",
        payload: { queue_prompt_id: "prompt.model.1", kind: terminalKind },
      });
      expect(
        (coordinatorBodies[2] as { payload: { artifact: unknown } }).payload
          .artifact,
      ).toBeNull();
      expect(refusedInvalidArtifact).toBe(false);
      expect(fixture.sideEffectCounts().queues).toBe(1);
    },
  );

  it("leaves aggregate prepare rollback to the backend after queue acceptance", async () => {
    const productionActions: string[] = [];
    const coordinatorActions: string[] = [];
    let initialProduction!: Record<string, unknown>;
    const { fixture, container } = await loadEntry(async (path, init) => {
      const body = JSON.parse(String(init.body)) as { action?: string };
      if (path === "/h3-context/v1/production/action") {
        productionActions.push(String(body.action));
        if (body.action === "create_workspace_from_context")
          return {
            ok: true,
            status: 201,
            json: async () => initialProduction,
          };
        if (body.action === "release_workspace")
          return { ok: true, status: 204, json: async () => ({}) };
      }
      if (path === "/h3-context/v1/generation/coordinator") {
        coordinatorActions.push(String(body.action));
        return { ok: false, status: 503, json: async () => ({}) };
      }
      return { ok: false, status: 503, json: async () => ({}) };
    });
    const managedFixture = managedEntryFixture(
      appModeFingerprint(fixture.app.graph.serialize()),
      fixture.workflowState().activeWorkflow,
      fixture.app.graph.serialize(),
    );
    initialProduction = managedFixture.production;
    fixture.setQueuePromptIds(["prompt.model.1"]);
    appModeStartHarness.start = async (_inputs, options = {}) => {
      const authority = await options.prepareManaged!(
        managedFixture.preparation,
      );
      authority.bindCanvasIdentity?.(
        managedFixture.preparation.managedIdentity,
      );
      authority.onQueueSubmitted();
      const queued = await fixture.api.queuePrompt(-1, {
        output: {
          "17": {
            class_type: "comfyui_h3_context.H3Context.ProductShell",
            inputs: {},
          },
          "6": { class_type: "MiniMaxH3ImageToVideo", inputs: {} },
          "43": { class_type: "SaveVideo", inputs: {} },
        },
        workflow: structuredClone(
          managedFixture.preparation.bootstrap.workflow,
        ),
      });
      const queueResult = acceptedQueueReceipt(queued.prompt_id, queued.number);
      const result: AppModeStartResult = {
        queueResult,
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint:
          managedFixture.preparation.observation.graph_fingerprint,
        compiledPromptFingerprint:
          managedFixture.planned.eligible_commands[0]!
            .compiled_prompt_fingerprint,
        queuePromptId: queueResult.prompt_id,
        route: "existing",
      };
      try {
        await authority.onQueueAccepted(result);
      } catch (error) {
        await authority.onQueueFailed("ambiguous");
        throw error;
      }
      throw new Error("managed preparation unexpectedly returned");
    };

    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
      { target: { value: "8" } },
    );
    await screen.findByText("Delivers 8 s (192 frames).");
    await clickReadyAppModeStart();
    act(() =>
      fixture.dispatchProjection("17", {
        promptId: "prompt.model.1",
        workspaceId: managedFixture.workspaceId,
        requestedSeconds: 8,
        taskMode: "t2va",
      }),
    );
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="error"]'),
      ).not.toBeNull(),
    );

    expect(fixture.sideEffectCounts().queues).toBe(1);
    expect(coordinatorActions).toEqual(["prepare_managed_run"]);
    expect(productionActions).toEqual([]);
    expect(
      window.sessionStorage.getItem(
        "h3.context.production.workspace_handle.v1",
      ),
    ).toBeNull();

    const surroundingGraph = fixture.app.graph.serialize() as Json;
    surroundingGraph.extra = { synthetic_host_metadata: true };
    fixture.setGraph(surroundingGraph);
    act(() =>
      (
        fixture.registeredExtension() as ReturnType<
          EntryHostFixture["registeredExtension"]
        > & { loadedGraphNode?: () => void }
      ).loadedGraphNode?.(),
    );
    act(() =>
      fixture.dispatchProjection("17", {
        promptId: "prompt.model.1",
        workspaceId: managedFixture.workspaceId,
        requestedSeconds: 8,
        taskMode: "t2va",
      }),
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(
      container.querySelector('[data-shell-status="projected"]'),
    ).toBeNull();
    expect(
      container.querySelector('[data-shell-status="interactive"]') ??
        container.querySelector('[data-shell-status="error"]'),
    ).not.toBeNull();
  });

  it("refuses a mismatched Production projection returned by aggregate prepare", async () => {
    const productionActions: string[] = [];
    let mismatchedProduction!: Record<string, unknown>;
    const { fixture, container } = await loadEntry(async (path, init) => {
      const body = JSON.parse(String(init.body)) as { action?: string };
      if (path === "/h3-context/v1/production/action")
        productionActions.push(String(body.action));
      if (
        path === "/h3-context/v1/generation/coordinator" &&
        body.action === "prepare_managed_run"
      )
        return {
          ok: true,
          status: 200,
          json: async () =>
            managedCoordinatorResponse(
              "prepared",
              managedFixture.planned,
              mismatchedProduction,
            ),
        };
      return { ok: false, status: 503, json: async () => ({}) };
    });
    const managedFixture = managedEntryFixture(
      appModeFingerprint(fixture.app.graph.serialize()),
      fixture.workflowState().activeWorkflow,
      fixture.app.graph.serialize(),
    );
    mismatchedProduction = {
      ...managedFixture.production,
      workspace_id: managedFixture.workspaceId,
    };
    fixture.setQueuePromptIds(["prompt.model.1"]);
    appModeStartHarness.start = async (_inputs, options = {}) => {
      const authority = await options.prepareManaged!(
        managedFixture.preparation,
      );
      authority.bindCanvasIdentity?.(
        managedFixture.preparation.managedIdentity,
      );
      authority.onQueueSubmitted();
      const queued = await fixture.api.queuePrompt(-1, {
        output: {
          "17": {
            class_type: "comfyui_h3_context.H3Context.ProductShell",
            inputs: {},
          },
          "6": { class_type: "MiniMaxH3ImageToVideo", inputs: {} },
          "43": { class_type: "SaveVideo", inputs: {} },
        },
        workflow: structuredClone(
          managedFixture.preparation.bootstrap.workflow,
        ),
      });
      const queueResult = acceptedQueueReceipt(queued.prompt_id, queued.number);
      const result: AppModeStartResult = {
        queueResult,
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint:
          managedFixture.preparation.observation.graph_fingerprint,
        compiledPromptFingerprint:
          managedFixture.planned.eligible_commands[0]!
            .compiled_prompt_fingerprint,
        queuePromptId: queueResult.prompt_id,
        route: "existing",
      };
      try {
        await authority.onQueueAccepted(result);
      } catch (error) {
        await authority.onQueueFailed("ambiguous");
        throw error;
      }
      throw new Error("mismatched Production unexpectedly returned");
    };

    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
      { target: { value: "8" } },
    );
    await screen.findByText("Delivers 8 s (192 frames).");
    await clickReadyAppModeStart();
    act(() =>
      fixture.dispatchProjection("17", {
        promptId: "prompt.model.1",
        workspaceId: managedFixture.workspaceId,
        requestedSeconds: 8,
        taskMode: "t2va",
      }),
    );
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="error"]'),
      ).not.toBeNull(),
    );

    expect(fixture.sideEffectCounts().queues).toBe(1);
    expect(productionActions).toEqual([]);
    const { managedJournal } = await import("../src/state/managedJournal");
    expect(managedJournal.snapshot()).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: "error",
          name: "app_mode",
          code: "ambiguous_host_ownership",
        }),
      ]),
    );
  });

  it.each([
    [
      "error",
      "execution_failed",
      "The H3 execution failed on the host.",
      "after_binding",
    ],
    [
      "interrupted",
      "execution_interrupted",
      "The H3 execution was interrupted on the host.",
      "after_binding",
    ],
    [
      "error",
      "execution_failed",
      "The H3 execution failed on the host.",
      "before_binding",
    ],
    [
      "interrupted",
      "execution_interrupted",
      "The H3 execution was interrupted on the host.",
      "before_binding",
    ],
  ] as const)(
    "preserves an accepted bootstrap %s terminal as %s %s",
    async (terminal, expectedCode, expectedMessage, timing) => {
      let managedFixture!: ReturnType<typeof managedEntryFixture>;
      const { fixture } = await loadEntry();
      managedFixture = managedEntryFixture(
        appModeFingerprint(fixture.app.graph.serialize()),
        fixture.workflowState().activeWorkflow,
        fixture.app.graph.serialize(),
      );
      fixture.setQueuePromptIds(["prompt.model.terminal"]);
      appModeStartHarness.start = async (_inputs, options = {}) => {
        const authority = await options.prepareManaged!(
          managedFixture.preparation,
        );
        authority.bindCanvasIdentity?.(
          managedFixture.preparation.managedIdentity,
        );
        authority.onQueueSubmitted();
        const queued = await fixture.api.queuePrompt(-1, {
          output: {
            "17": {
              class_type: "comfyui_h3_context.H3Context.ProductShell",
              inputs: {},
            },
            "6": { class_type: "MiniMaxH3ImageToVideo", inputs: {} },
            "43": { class_type: "SaveVideo", inputs: {} },
          },
          workflow: structuredClone(
            managedFixture.preparation.bootstrap.workflow,
          ),
        });
        const queueResult = acceptedQueueReceipt(
          queued.prompt_id,
          queued.number,
        );
        const result: AppModeStartResult = {
          queueResult,
          ...OWNED_RESULT_IDENTITY,
          graphFingerprint:
            managedFixture.preparation.observation.graph_fingerprint,
          compiledPromptFingerprint:
            managedFixture.planned.eligible_commands[0]!
              .compiled_prompt_fingerprint,
          queuePromptId: queueResult.prompt_id,
          route: "existing",
        };
        if (timing === "before_binding")
          fixture.dispatchExecutionTerminal(terminal, "prompt.model.terminal");
        await authority.onQueueAccepted(result);
        throw new Error("terminal bootstrap unexpectedly returned");
      };

      fireEvent.change(
        screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
        { target: { value: "8" } },
      );
      await screen.findByText("Delivers 8 s (192 frames).");
      await clickReadyAppModeStart();
      await waitFor(() => expect(fixture.sideEffectCounts().queues).toBe(1));
      const { managedJournal } = await import("../src/state/managedJournal");
      await waitFor(() =>
        expect(managedJournal.snapshot()).toEqual(
          expect.arrayContaining([
            expect.objectContaining({
              kind: "stage",
              name: "bootstrap_prompt_bound",
            }),
          ]),
        ),
      );
      if (timing === "after_binding")
        act(() =>
          fixture.dispatchExecutionTerminal(terminal, "prompt.model.terminal"),
        );

      await screen.findByText(expectedMessage);
      const entries = managedJournal.snapshot();
      expect(entries).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            kind: "error",
            name: "app_mode",
            code: expectedCode,
          }),
          expect.objectContaining({
            kind: "state",
            name: "error",
            code: expectedCode,
          }),
        ]),
      );
      expect(entries).not.toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            kind: "error",
            code: "ambiguous_host_ownership",
          }),
        ]),
      );
    },
  );

  it.each(["retry", "edit"] as const)(
    "settles an accepted pre-prepare failure into the empty project through %s",
    async (recovery) => {
      const projectHandle = `pw_${"p".repeat(43)}`;
      const projectId = "workspace_fast_terminal";
      const candidateId = "segment_fast_terminal";
      const bodies: Array<Record<string, unknown>> = [];
      let admissions = 0;
      let managedFixture!: ReturnType<typeof managedEntryFixture>;
      const projectWire = (
        status: "admitted" | "failed",
        attemptId: string,
        revision: number,
        wireCandidateId = candidateId,
      ) => {
        const material = {
          schema: "h3.context.production_accumulated_project.v1",
          workspace_handle: projectHandle,
          workspace_id: projectId,
          project_revision: revision,
          workspace: null,
          attempts: [
            {
              candidate_id: wireCandidateId,
              attempt_id: attemptId,
              member_segment_id: wireCandidateId,
              status,
              recovery: status === "failed" ? "retry" : null,
              committed: false,
            },
          ],
          capabilities: ["read", "admit_generation", "release_generation"],
        };
        return {
          ...material,
          project_fingerprint:
            productionAccumulatedProjectFingerprint(material),
        };
      };
      const { fixture } = await loadEntry(async (path, init) => {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        if (path !== "/h3-context/v1/production/action")
          return { ok: false, status: 503, json: async () => ({}) };
        bodies.push(body);
        if (body.action === "admit_generation_destination_v2") {
          admissions += 1;
          const payload = body.payload as Record<string, unknown>;
          const retrying = typeof payload.segment_id === "string";
          return {
            ok: true,
            status: admissions === 1 ? 201 : 200,
            json: async () =>
              projectWire(
                "admitted",
                admissions === 1 ? "attempt_fast_terminal" : "attempt_retry",
                admissions === 1 ? 1 : 3,
                admissions === 1 || retrying
                  ? candidateId
                  : "segment_new_ordinary_start",
              ),
          };
        }
        if (body.action === "settle_generation_destination_v2")
          return {
            ok: true,
            status: 200,
            json: async () => projectWire("failed", "attempt_fast_terminal", 2),
          };
        return { ok: false, status: 503, json: async () => ({}) };
      });
      managedFixture = managedEntryFixture(
        appModeFingerprint(fixture.app.graph.serialize()),
        recovery === "edit"
          ? fixture.workflowState().activeWorkflow
          : fixture.app.graph,
        fixture.app.graph.serialize(),
      );
      const replacementPreparation = Object.freeze({
        ...managedFixture.preparation,
        observation: Object.freeze({
          ...managedFixture.preparation.observation,
          route: "replace" as const,
        }),
      });
      let starts = 0;
      const replaceRoutes: Array<boolean | undefined> = [];
      fixture.setQueuePromptIds(["prompt.model.fast-terminal"]);
      appModeStartHarness.start = async (_inputs, options = {}) => {
        starts += 1;
        replaceRoutes.push(options.replaceExisting);
        const authority = await options.prepareManaged!(replacementPreparation);
        if (starts > 1)
          throw new AppModeError("cancelled", "stop after retry admission");
        authority.bindCanvasIdentity?.(replacementPreparation.managedIdentity);
        authority.onQueueSubmitted();
        const queued = await fixture.api.queuePrompt(-1, {
          output: {
            "17": {
              class_type: "comfyui_h3_context.H3Context.ProductShell",
              inputs: {},
            },
            "6": { class_type: "MiniMaxH3ImageToVideo", inputs: {} },
            "43": { class_type: "SaveVideo", inputs: {} },
          },
          workflow: structuredClone(replacementPreparation.bootstrap.workflow),
        });
        const queueResult = acceptedQueueReceipt(
          queued.prompt_id,
          queued.number,
        );
        fixture.dispatchExecutionTerminal("error", queued.prompt_id);
        await authority.onQueueAccepted({
          queueResult,
          ...OWNED_RESULT_IDENTITY,
          graphFingerprint:
            replacementPreparation.observation.graph_fingerprint,
          compiledPromptFingerprint:
            replacementPreparation.observation.compiled_prompt_fingerprint,
          queuePromptId: queueResult.prompt_id,
          route: "replace",
        });
        throw new Error("terminal bootstrap unexpectedly returned");
      };

      fireEvent.change(
        screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
        { target: { value: "8" } },
      );
      await screen.findByText("Delivers 8 s (192 frames).");
      await clickReadyAppModeStart();
      await screen.findByText("The H3 execution failed on the host.");
      expect(bodies.map((body) => body.action)).toEqual([
        "admit_generation_destination_v2",
        "settle_generation_destination_v2",
      ]);
      expect((bodies[1]?.payload as Record<string, unknown>).terminal).toBe(
        "failed",
      );

      fireEvent.click(screen.getByRole("button", { name: "Production" }));
      expect(
        await screen.findByText("No generated segments yet."),
      ).not.toBeNull();
      expect(await screen.findByText("Latest attempt: Failed.")).not.toBeNull();

      fireEvent.click(screen.getByRole("button", { name: "Context" }));
      if (recovery === "retry")
        fireEvent.click(
          screen.getByRole("button", { name: "Retry H3 App Mode" }),
        );
      else {
        fireEvent.click(
          screen.getByRole("button", { name: "Edit App Mode setup" }),
        );
        await clickReadyAppModeStart();
        await screen.findByText(
          "Canvas ready. Check its settings, then queue manually.",
        );
        expect(admissions).toBe(1);
        await clickReadyAppModeStart();
      }
      await waitFor(() => expect(admissions).toBe(2));
      if (recovery === "retry") {
        expect((bodies[2]?.payload as Record<string, unknown>).segment_id).toBe(
          candidateId,
        );
        expect(replaceRoutes).toEqual([undefined, undefined]);
      } else {
        expect(
          (bodies[2]?.payload as Record<string, unknown>).segment_id,
        ).toBeNull();
        expect(replaceRoutes).toEqual([undefined, undefined]);
      }
    },
  );

  it("submits a sidebar-default snapped 5-second managed start", async () => {
    const coordinatorActions: string[] = [];
    let managedFixture!: ReturnType<typeof managedEntryFixture>;
    let submitted!: EntryGenerationSequenceWire;
    const { fixture, container } = await loadEntry(async (path, init) => {
      const body = JSON.parse(String(init.body)) as { action?: string };
      if (path === "/h3-context/v1/production/action")
        return {
          ok: true,
          status: 201,
          json: async () => managedFixture.production,
        };
      if (path === "/h3-context/v1/generation/coordinator") {
        coordinatorActions.push(String(body.action));
        const response =
          body.action === "prepare_managed_run"
            ? managedCoordinatorResponse(
                "prepared",
                managedFixture.planned,
                managedProductionStateWire(
                  managedFixture.planned,
                  "planned",
                  managedFixture.planned,
                ),
              )
            : body.action === "submit_managed_run"
              ? managedCoordinatorResponse(
                  "submitted",
                  submitted,
                  managedProductionStateWire(
                    submitted,
                    "submitted",
                    managedFixture.planned,
                  ),
                )
              : undefined;
        if (response !== undefined)
          return { ok: true, status: 200, json: async () => response };
      }
      return { ok: false, status: 503, json: async () => ({}) };
    });
    managedFixture = managedEntryFixture(
      appModeFingerprint(fixture.app.graph.serialize()),
      fixture.workflowState().activeWorkflow,
      fixture.app.graph.serialize(),
      { effectiveMilliseconds: 5167, frameCount: 124 },
    );
    submitted = managedSequenceStateWire(managedFixture.planned, "submitted");
    fixture.setQueuePromptIds(["prompt.model.snapped"]);
    let accepted = false;
    let capturedInputs: AppModeInputs | undefined;
    appModeStartHarness.start = async (inputs, options = {}) => {
      capturedInputs = inputs;
      const authority = await options.prepareManaged!(
        managedFixture.preparation,
      );
      authority.bindCanvasIdentity?.(
        managedFixture.preparation.managedIdentity,
      );
      authority.onQueueSubmitted();
      const queued = await fixture.api.queuePrompt(-1, {
        output: {
          "17": {
            class_type: "comfyui_h3_context.H3Context.ProductShell",
            inputs: {},
          },
          "6": { class_type: "MiniMaxH3TextToVideo", inputs: {} },
          "43": { class_type: "SaveVideo", inputs: {} },
        },
        workflow: structuredClone(
          managedFixture.preparation.bootstrap.workflow,
        ),
      });
      const queueResult = acceptedQueueReceipt(queued.prompt_id, queued.number);
      const result: AppModeStartResult = {
        queueResult,
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint:
          managedFixture.preparation.observation.graph_fingerprint,
        compiledPromptFingerprint:
          managedFixture.planned.eligible_commands[0]!
            .compiled_prompt_fingerprint,
        queuePromptId: queueResult.prompt_id,
        route: "existing",
      };
      await authority.onQueueAccepted(result);
      accepted = true;
      return result;
    };

    await clickReadyAppModeStart();
    await waitFor(() => expect(fixture.sideEffectCounts().queues).toBe(1));
    expect(capturedInputs).toEqual({
      task_mode: "t2va",
      user_intent: "Describe the intended H3 shot.",
      duration_milliseconds: 5000,
      frame_count: 124,
    });
    const { managedJournal } = await import("../src/state/managedJournal");
    await waitFor(() =>
      expect(managedJournal.snapshot()).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            kind: "stage",
            name: "bootstrap_prompt_bound",
          }),
        ]),
      ),
    );
    act(() =>
      fixture.dispatchProjection("17", {
        promptId: "prompt.model.snapped",
        workspaceId: managedFixture.workspaceId,
        requestedSeconds: 5,
        taskMode: "t2va",
        effectiveDurationMilliseconds: 5167,
        effectiveFrameCount: 124,
      }),
    );

    await waitFor(() =>
      expect(
        accepted ||
          container.querySelector('[data-shell-status="error"]') !== null,
      ).toBe(true),
    );
    expect(container.querySelector('[data-shell-status="error"]')).toBeNull();
    expect(accepted).toBe(true);
    expect(coordinatorActions).toEqual([
      "prepare_managed_run",
      "submit_managed_run",
    ]);
  });

  it("B-M1605-EXIST-02 submits an existing I2VA graph queued with the Sidebar default task mode", async () => {
    const coordinatorActions: string[] = [];
    let managedFixture!: ReturnType<typeof managedEntryFixture>;
    let submitted!: EntryGenerationSequenceWire;
    const { fixture, container } = await loadEntry(async (path, init) => {
      const body = JSON.parse(String(init.body)) as { action?: string };
      if (path === "/h3-context/v1/production/action")
        return {
          ok: true,
          status: 201,
          json: async () => managedFixture.production,
        };
      if (path === "/h3-context/v1/generation/coordinator") {
        coordinatorActions.push(String(body.action));
        const response =
          body.action === "prepare_managed_run"
            ? managedCoordinatorResponse(
                "prepared",
                managedFixture.planned,
                managedProductionStateWire(
                  managedFixture.planned,
                  "planned",
                  managedFixture.planned,
                ),
              )
            : body.action === "submit_managed_run"
              ? managedCoordinatorResponse(
                  "submitted",
                  submitted,
                  managedProductionStateWire(
                    submitted,
                    "submitted",
                    managedFixture.planned,
                  ),
                )
              : undefined;
        if (response !== undefined)
          return { ok: true, status: 200, json: async () => response };
      }
      return { ok: false, status: 503, json: async () => ({}) };
    });
    managedFixture = managedEntryFixture(
      appModeFingerprint(fixture.app.graph.serialize()),
      fixture.workflowState().activeWorkflow,
      fixture.app.graph.serialize(),
    );
    // The backend plans from the executed graph's own Request, which is I2VA.
    managedFixture.planned.eligible_commands[0]!.task_mode = "i2va";
    (
      managedFixture.production.segments as Array<Record<string, unknown>>
    )[0]!.task_mode = "i2va";
    submitted = managedSequenceStateWire(managedFixture.planned, "submitted");
    fixture.setQueuePromptIds(["prompt.model.i2va"]);
    let accepted = false;
    let capturedInputs: AppModeInputs | undefined;
    appModeStartHarness.start = async (inputs, options = {}) => {
      capturedInputs = inputs;
      const authority = await options.prepareManaged!(
        managedFixture.preparation,
      );
      authority.bindCanvasIdentity?.(
        managedFixture.preparation.managedIdentity,
      );
      authority.onQueueSubmitted();
      const queued = await fixture.api.queuePrompt(-1, {
        output: {
          "17": {
            class_type: "comfyui_h3_context.H3Context.ProductShell",
            inputs: {},
          },
          "6": { class_type: "MiniMaxH3ImageToVideo", inputs: {} },
          "43": { class_type: "SaveVideo", inputs: {} },
        },
        workflow: structuredClone(
          managedFixture.preparation.bootstrap.workflow,
        ),
      });
      const queueResult = acceptedQueueReceipt(queued.prompt_id, queued.number);
      const result: AppModeStartResult = {
        queueResult,
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint:
          managedFixture.preparation.observation.graph_fingerprint,
        compiledPromptFingerprint:
          managedFixture.planned.eligible_commands[0]!
            .compiled_prompt_fingerprint,
        queuePromptId: queueResult.prompt_id,
        route: "existing",
      };
      await authority.onQueueAccepted(result);
      accepted = true;
      return result;
    };

    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
      { target: { value: "8" } },
    );
    await screen.findByText("Delivers 8 s (192 frames).");
    await clickReadyAppModeStart();
    await waitFor(() => expect(fixture.sideEffectCounts().queues).toBe(1));
    expect(capturedInputs?.task_mode).toBe("t2va");
    const { managedJournal } = await import("../src/state/managedJournal");
    await waitFor(() =>
      expect(managedJournal.snapshot()).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            kind: "stage",
            name: "bootstrap_prompt_bound",
          }),
        ]),
      ),
    );
    act(() =>
      fixture.dispatchProjection("17", {
        promptId: "prompt.model.i2va",
        workspaceId: managedFixture.workspaceId,
        requestedSeconds: 8,
        taskMode: "i2va",
      }),
    );

    await waitFor(() =>
      expect(
        accepted ||
          container.querySelector('[data-shell-status="error"]') !== null,
      ).toBe(true),
    );
    expect(container.querySelector('[data-shell-status="error"]')).toBeNull();
    expect(accepted).toBe(true);
    expect(coordinatorActions).toEqual([
      "prepare_managed_run",
      "submit_managed_run",
    ]);
  });

  it("releases a late admission response after cancellation before queue", async () => {
    type FetchResponse = Readonly<{
      ok: boolean;
      status: number;
      json(): Promise<unknown>;
    }>;
    let resolveAdmission: ((response: FetchResponse) => void) | undefined;
    const pendingAdmission = new Promise<FetchResponse>((resolve) => {
      resolveAdmission = resolve;
    });
    let managedFixture!: ReturnType<typeof managedEntryFixture>;
    const { fixture } = await loadEntry(async (path, init) => {
      const body = JSON.parse(String(init.body)) as { action?: string };
      if (
        path === "/h3-context/v1/production/action" &&
        body.action === "admit_generation_destination_v2"
      )
        return pendingAdmission;
      if (
        path === "/h3-context/v1/production/action" &&
        body.action === "release_generation_destination_v2"
      )
        return { ok: true, status: 204, json: async () => ({}) };
      return { ok: false, status: 503, json: async () => ({}) };
    });
    managedFixture = managedEntryFixture(
      appModeFingerprint(fixture.app.graph.serialize()),
      fixture.workflowState().activeWorkflow,
      fixture.app.graph.serialize(),
    );
    appModeStartHarness.start = async (_inputs, options = {}) => {
      await options.prepareManaged!({
        ...managedFixture.preparation,
        observation: {
          ...managedFixture.preparation.observation,
          route: "replace",
        },
      });
      throw new Error("cancelled preparation unexpectedly returned");
    };

    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
      { target: { value: "8" } },
    );
    await screen.findByText("Delivers 8 s (192 frames).");
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        fixture.productionFetchBodies().map((body) => body.action),
      ).toEqual(["admit_generation_destination_v2"]),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel App Mode" }));
    const emptyProject = {
      schema: "h3.context.production_accumulated_project.v1",
      workspace_handle: `pw_${"a".repeat(43)}`,
      workspace_id: "workspace_late_admission",
      project_revision: 1,
      workspace: null,
      attempts: [
        {
          candidate_id: "segment_late_admission",
          attempt_id: "attempt_late_admission",
          member_segment_id: "segment_late_admission",
          status: "admitted",
          recovery: null,
          committed: false,
        },
      ],
      capabilities: ["read", "admit_generation", "release_generation"],
    };
    resolveAdmission?.({
      ok: true,
      status: 201,
      json: async () => ({
        ...emptyProject,
        project_fingerprint:
          productionAccumulatedProjectFingerprint(emptyProject),
      }),
    });

    await waitFor(() =>
      expect(
        fixture.productionFetchBodies().map((body) => body.action),
      ).toEqual([
        "admit_generation_destination_v2",
        "release_generation_destination_v2",
      ]),
    );
    expect(fixture.sideEffectCounts().queues).toBe(0);
  });

  it("creates no provisional owner when the one managed queue is explicitly rejected", async () => {
    const productionActions: string[] = [];
    const coordinatorActions: string[] = [];
    let managedFixture!: ReturnType<typeof managedEntryFixture>;
    const { fixture, container } = await loadEntry(async (path, init) => {
      const body = JSON.parse(String(init.body)) as { action?: string };
      if (path === "/h3-context/v1/production/action") {
        productionActions.push(String(body.action));
        if (body.action === "create_workspace_from_context")
          return {
            ok: true,
            status: 201,
            json: async () => managedFixture.production,
          };
        if (body.action === "release_workspace")
          return { ok: true, status: 204, json: async () => ({}) };
      }
      if (path === "/h3-context/v1/generation/coordinator")
        // This test asserts the coordinator is never called at all, so every action falls through
        // to the 503 below. Serving the legacy sequence actions a success here would hand a
        // regressed client a working answer instead of failing the assertion it exists to make.
        coordinatorActions.push(String(body.action));
      return { ok: false, status: 503, json: async () => ({}) };
    });
    managedFixture = managedEntryFixture(
      appModeFingerprint(fixture.app.graph.serialize()),
      fixture.workflowState().activeWorkflow,
      fixture.app.graph.serialize(),
    );
    appModeStartHarness.start = async (_inputs, options = {}) => {
      options.onQueueSeamObserved?.({
        functionName: "forwardQueue",
        arity: 2,
        changedSinceControllerCreation: true,
      });
      const authority = await options.prepareManaged!(
        managedFixture.preparation,
      );
      await authority.onQueueFailed("rejected");
      throw new AppModeError("queue_failed", "synthetic native queue refusal");
    };

    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Clip duration (seconds)" }),
      { target: { value: "8" } },
    );
    await screen.findByText("Delivers 8 s (192 frames).");
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="error"]'),
      ).not.toBeNull(),
    );

    expect(fixture.sideEffectCounts().queues).toBe(0);
    expect(coordinatorActions).toEqual([]);
    expect(productionActions).toEqual([]);
    const { managedJournal } = await import("../src/state/managedJournal");
    expect(managedJournal.snapshot()).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: "queue_seam",
          name: "queue_prompt",
          function_name: "forwardQueue",
          arity: 2,
          changed_since_controller_creation: true,
        }),
      ]),
    );
  });

  it("ends a rejected queue while Production cleanup is still pending", async () => {
    let resolveCleanup!: (response: {
      ok: boolean;
      status: number;
      json: () => Promise<object>;
    }) => void;
    const cleanupResponse = new Promise<{
      ok: boolean;
      status: number;
      json: () => Promise<object>;
    }>((resolve) => {
      resolveCleanup = resolve;
    });
    const emptyProject = {
      schema: "h3.context.production_accumulated_project.v1",
      workspace_handle: `pw_${"a".repeat(43)}`,
      workspace_id: "workspace_rejected_queue",
      project_revision: 1,
      workspace: null,
      attempts: [
        {
          candidate_id: "segment_rejected_queue",
          attempt_id: "attempt_rejected_queue",
          member_segment_id: "segment_rejected_queue",
          status: "admitted",
          recovery: null,
          committed: false,
        },
      ],
      capabilities: ["read", "admit_generation", "release_generation"],
    };
    const { fixture, container } = await loadEntry(async (path, init) => {
      const body = JSON.parse(String(init.body)) as { action?: string };
      if (
        path === "/h3-context/v1/production/action" &&
        body.action === "admit_generation_destination_v2"
      )
        return {
          ok: true,
          status: 201,
          json: async () => ({
            ...emptyProject,
            project_fingerprint:
              productionAccumulatedProjectFingerprint(emptyProject),
          }),
        };
      if (
        path === "/h3-context/v1/production/action" &&
        body.action === "release_generation_destination_v2"
      )
        return cleanupResponse;
      return { ok: false, status: 503, json: async () => ({}) };
    });
    const managedFixture = managedEntryFixture(
      appModeFingerprint(fixture.app.graph.serialize()),
      fixture.workflowState().activeWorkflow,
      fixture.app.graph.serialize(),
    );
    appModeStartHarness.start = async (_inputs, options = {}) => {
      const authority = await options.prepareManaged!({
        ...managedFixture.preparation,
        observation: {
          ...managedFixture.preparation.observation,
          route: "replace",
        },
      });
      authority.onQueueSubmitted();
      await authority.onQueueFailed("rejected");
      throw new AppModeError("queue_failed", "synthetic native model refusal", {
        kind: "queue_rejected",
        classTypes: ["CLIPLoader"],
        errorTypes: ["value_not_in_list"],
      });
    };
    await clickReadyAppModeStart();
    try {
      await waitFor(() =>
        expect(
          container.querySelector('[data-shell-status="error"]'),
        ).not.toBeNull(),
      );
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).toBeNull();
      expect(
        screen.getByRole<HTMLButtonElement>("button", {
          name: "Retry H3 App Mode",
        }).disabled,
      ).toBe(false);
    } finally {
      await act(async () => {
        resolveCleanup({ ok: true, status: 204, json: async () => ({}) });
        await cleanupResponse;
      });
    }
  });
});

describe("entry Production Generate lifecycle", () => {
  it("derives pending and submitted dispositions across real tab remounts", async () => {
    const { fixture, container, sequence } = await loadGenerationReadyEntry();
    const deferred = deferredAppModeStart();
    appModeStartHarness.start = () => deferred.promise;

    fireEvent.click(screen.getByRole("button", { name: "Generate segment 1" }));
    await screen.findByText("Generation submission pending for segment 1.");

    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    act(() => tab.render(container));
    await screen.findByText("Generation submission pending for segment 1.");

    await act(async () => {
      deferred.resolve(appModeResult(sequence));
      await deferred.promise;
      await Promise.resolve();
    });
    act(() => tab.destroy());
    act(() => tab.render(container));

    await screen.findByText("Generation submitted for segment 1.");
    expect(
      screen.queryByRole("button", { name: "Generate segment 1" }),
    ).toBeNull();
  });

  it("quarantines a resolved old key across navigation, unmount, and a fresh publication", async () => {
    const { fixture, container, sequence, publish } =
      await loadGenerationReadyEntry();
    const deferred = deferredAppModeStart();
    appModeStartHarness.start = () => deferred.promise;

    fireEvent.click(screen.getByRole("button", { name: "Generate segment 1" }));
    await screen.findByText("Generation submission pending for segment 1.");

    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    const tab = fixture.registeredTab();
    act(() => tab.destroy());

    await act(async () => {
      deferred.resolve(appModeResult(sequence));
      await deferred.promise;
      await Promise.resolve();
    });
    act(() => tab.render(container));
    expect(
      screen
        .getByRole("button", { name: "Context" })
        .getAttribute("aria-current"),
    ).toBe("page");

    const nextSequence = entryGenerationSequenceWire(
      "c",
      2,
      sequence.eligible_commands[0]!.graph_fingerprint,
    );
    publish(nextSequence, "generation-prompt-1");
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    expect(
      await screen.findByRole("button", { name: "Generate segment 1" }),
    ).toBeTruthy();
    expect(
      screen.queryByText("Generation submitted for segment 1."),
    ).toBeNull();
  });

  it("quarantines rejection after authority release, navigation, and unmount", async () => {
    const { fixture, container } = await loadGenerationReadyEntry();
    const deferred = deferredAppModeStart();
    appModeStartHarness.start = () => deferred.promise;

    fireEvent.click(screen.getByRole("button", { name: "Generate segment 1" }));
    await screen.findByText("Generation submission pending for segment 1.");

    fireEvent.click(screen.getByRole("button", { name: "Release workspace" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm release" }));
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "Context" })
          .getAttribute("aria-current"),
      ).toBe("page"),
    );
    const tab = fixture.registeredTab();
    act(() => tab.destroy());

    await act(async () => {
      deferred.reject(new Error("synthetic generation rejection"));
      await deferred.promise.catch(() => undefined);
      await Promise.resolve();
    });
    act(() => tab.render(container));
    expect(
      screen
        .getByRole("button", { name: "Context" })
        .getAttribute("aria-current"),
    ).toBe("page");
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    expect(
      screen.queryByRole("button", { name: "Generate segment 1" }),
    ).toBeNull();
    expect(
      screen.queryByText("Generation submission failed for segment 1."),
    ).toBeNull();
  });
});

describe("entry Production proposal lifecycle", () => {
  it("keeps proposal ownership at Context and acceptance leaves Production unchanged", async () => {
    const proposalBodies: Array<Record<string, unknown>> = [];
    const { fixture } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (body.action === "create_workspace_from_context")
        return {
          ok: true,
          status: 201,
          json: async () => productionProjectionWire(),
        };
      proposalBodies.push(body);
      const terminal = body.action === "proposal_accept" ? "accepted" : null;
      return {
        ok: true,
        status: 200,
        json: async () =>
          semanticProposalResultWire(
            String(body.expected_report_fingerprint),
            terminal,
          ),
      };
    });
    act(() =>
      fixture.dispatchProjection("17", {
        semanticProposalReview: semanticProposalHandleWire(),
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    fireEvent.click(
      await screen.findByRole("button", {
        name: /^Understand segment( \d+)?$/,
      }),
    );
    await screen.findByText("Entry-owned bounded proposal summary");
    fireEvent.click(screen.getByRole("button", { name: "Accept proposal" }));
    await screen.findByText(/Production is unchanged/i);

    expect(proposalBodies.map((body) => body.action)).toEqual([
      "proposal_read",
      "proposal_accept",
    ]);
    expect(proposalBodies[0]).toMatchObject({
      workspace_id: "ws_0123456789abcdefghijklmnopqrstuv",
      expected_revision: 1,
      payload: { review_id: `review_${"r".repeat(32)}` },
    });
    expect(fixture.productionFetchBodies()).toHaveLength(1);
  });

  it("keeps the original proposal row through a mounted no-source append", async () => {
    const { fixture, container } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (body.action === "create_workspace_from_context")
        return {
          ok: true,
          status: 201,
          json: async () => productionProjectionWire(1, 1),
        };
      if (body.action === "add_segment_from_context")
        return {
          ok: true,
          status: 200,
          json: async () => productionProjectionWire(2, 2),
        };
      return { ok: false, status: 422, json: async () => ({}) };
    });
    act(() =>
      fixture.dispatchProjection("17", {
        semanticProposalReview: semanticProposalHandleWire(),
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("button", { name: "Select segment 1" });
    act(() =>
      fixture.dispatchProjection("17", { promptId: "prompt-no-source-append" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Add current Context" }),
    );

    await screen.findByRole("button", { name: "Select segment 2" });
    expect(container.querySelectorAll(".h3-semantic-review")).toHaveLength(1);
    expect(
      screen.getAllByText(
        "No intent proposal was supplied for the selected segments.",
      ),
    ).toHaveLength(1);
  });

  it("invalidates the mounted target after a no-source replacement", async () => {
    const { fixture, container } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (body.action === "create_workspace_from_context")
        return {
          ok: true,
          status: 201,
          json: async () => productionProjectionWire(1, 1),
        };
      if (body.action === "replace_segment_from_context")
        return {
          ok: true,
          status: 200,
          json: async () => productionProjectionWire(2, 1),
        };
      return { ok: false, status: 422, json: async () => ({}) };
    });
    act(() =>
      fixture.dispatchProjection("17", {
        semanticProposalReview: semanticProposalHandleWire(),
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("button", { name: "Select segment 1" });
    expect(container.querySelectorAll(".h3-semantic-review")).toHaveLength(1);
    act(() =>
      fixture.dispatchProjection("17", {
        promptId: "prompt-no-source-replace",
      }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "Replace segment 1 with current Context",
      }),
    );

    expect(
      await screen.findByText(
        "No intent proposal was supplied for the selected segments.",
      ),
    ).toBeDefined();
    expect(container.querySelectorAll(".h3-semantic-review")).toHaveLength(0);
  });

  // IMPORTANT: this mounted lifecycle crosses several independently bounded async
  // transitions; keep a suite-contention envelope without weakening any wait.
  it("drives Resolve and Reject through the mounted Production row", async () => {
    const proposalBodies: Array<Record<string, unknown>> = [];
    const { fixture } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (body.action === "create_workspace_from_context")
        return {
          ok: true,
          status: 201,
          json: async () => productionProjectionWire(),
        };
      proposalBodies.push(body);
      const reportFingerprint = String(body.expected_report_fingerprint);
      let result: Record<string, unknown>;
      if (body.action === "proposal_read")
        result = resolvableProposalResultWire(reportFingerprint);
      else if (body.action === "proposal_resolve")
        result = resolvedProposalResultWire(reportFingerprint);
      else {
        result = semanticProposalResultWire(reportFingerprint, "rejected");
        const review = result.review as Record<string, unknown>;
        review.revision = 3;
        review.workspace_revision = 3;
      }
      return { ok: true, status: 200, json: async () => result };
    });
    act(() =>
      fixture.dispatchProjection("17", {
        semanticProposalReview: semanticProposalHandleWire(),
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    fireEvent.click(
      await screen.findByRole("button", {
        name: /^Understand segment( \d+)?$/,
      }),
    );
    const resolution = await screen.findByRole("textbox", {
      name: /Synthetic camera choice/,
    });
    fireEvent.change(resolution, {
      target: { value: "Keep the safe framing" },
    });
    const submitClarifications = screen.getByRole<HTMLButtonElement>("button", {
      name: "Submit clarifications",
    });
    await waitFor(() => expect(submitClarifications.disabled).toBe(false));
    fireEvent.click(submitClarifications);
    await waitFor(() =>
      expect(proposalBodies.map((body) => body.action)).toEqual([
        "proposal_read",
        "proposal_resolve",
      ]),
    );
    const reject = screen.getByRole<HTMLButtonElement>("button", {
      name: "Reject proposal",
    });
    await waitFor(() => expect(reject.disabled).toBe(false));
    fireEvent.click(reject);
    await waitFor(() =>
      expect(proposalBodies.map((body) => body.action)).toEqual([
        "proposal_read",
        "proposal_resolve",
        "proposal_reject",
      ]),
    );
    expect(await screen.findByText(/rejected/i)).toBeTruthy();
  }, 15_000);

  it("stops a multi-row queue across navigation before configure and unmount", async () => {
    const pending: Array<
      (value: { ok: boolean; status: number; json(): Promise<unknown> }) => void
    > = [];
    let productionRevision = 0;
    let proposalReadCount = 0;
    const { fixture, container } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (
        body.action === "create_workspace_from_context" ||
        body.action === "add_segment_from_context"
      ) {
        productionRevision += 1;
        return {
          ok: true,
          status: body.action === "create_workspace_from_context" ? 201 : 200,
          json: async () =>
            productionProjectionWire(productionRevision, productionRevision),
        };
      }
      proposalReadCount += 1;
      const result = {
        ok: true,
        status: 200,
        json: async () =>
          semanticProposalResultWire(
            String(body.expected_report_fingerprint),
            null,
            `queued proposal ${proposalReadCount}`,
          ),
      };
      if (proposalReadCount > 2) return result;
      return new Promise((resolve) => pending.push(resolve));
    });
    act(() =>
      fixture.dispatchProjection("17", {
        semanticProposalReview: semanticProposalHandleWire(),
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    await screen.findByRole("button", { name: "Select segment 1" });
    fireEvent.click(
      screen.getByRole("button", { name: "Add current Context" }),
    );
    await screen.findByRole("button", { name: "Select segment 2" });
    fireEvent.click(
      screen.getByRole("button", { name: "Add current Context" }),
    );
    await screen.findByRole("button", { name: "Select segment 3" });
    fireEvent.click(
      screen.getByRole("button", {
        name: /^Understand selected \(\d+\)$/,
      }),
    );
    await waitFor(() => expect(proposalReadCount).toBe(2));

    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    await act(async () => {
      for (const resolve of pending)
        resolve({
          ok: true,
          status: 200,
          json: async () =>
            semanticProposalResultWire(
              semanticHash("d"),
              null,
              "late queued proposal",
            ),
        });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(proposalReadCount).toBe(2);

    const extension = fixture.registeredExtension() as {
      beforeConfigureGraph?: () => void;
    };
    act(() => extension.beforeConfigureGraph?.());
    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    act(() => tab.render(container));
    expect(screen.queryByText("late queued proposal")).toBeNull();
  });

  it("quarantines a late proposal response across navigation and unmount", async () => {
    let resolveProposal:
      | ((value: {
          ok: boolean;
          status: number;
          json(): Promise<unknown>;
        }) => void)
      | undefined;
    const pending = new Promise<{
      ok: boolean;
      status: number;
      json(): Promise<unknown>;
    }>((resolve) => {
      resolveProposal = resolve;
    });
    let proposalFingerprint = "";
    const { fixture, container } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (body.action === "create_workspace_from_context")
        return {
          ok: true,
          status: 201,
          json: async () => productionProjectionWire(),
        };
      proposalFingerprint = String(body.expected_report_fingerprint);
      return pending;
    });
    act(() =>
      fixture.dispatchProjection("17", {
        semanticProposalReview: semanticProposalHandleWire(),
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    fireEvent.click(
      await screen.findByRole("button", {
        name: /^Understand segment( \d+)?$/,
      }),
    );
    await screen.findByText(/Loading the current proposal review/i);
    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    resolveProposal?.({
      ok: true,
      status: 200,
      json: async () =>
        semanticProposalResultWire(
          proposalFingerprint,
          null,
          "Late proposal must remain quarantined",
        ),
    });
    await act(async () => Promise.resolve());
    act(() => tab.render(container));
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    expect(
      screen.queryByText("Late proposal must remain quarantined"),
    ).toBeNull();
  });
});

describe("entry projected workspace remount", () => {
  it("rehydrates the projected requested duration when setup editing begins", async () => {
    const { container } = await loadProjectedEntry(8);

    fireEvent.click(
      screen.getByRole("button", { name: "Edit App Mode setup" }),
    );
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="editing_setup"]'),
      ).not.toBeNull(),
    );
    fireEvent.click(screen.getByRole("tab", { name: "Intent / Mode" }));

    expect(
      (
        screen.getByRole("spinbutton", {
          name: /duration \(seconds\)/i,
        }) as HTMLInputElement
      ).value,
    ).toBe("8");
  });

  it("returns to setup and back without graph, queue, or backend side effects", async () => {
    const { fixture, container } = await loadProjectedEntry();
    const tab = fixture.registeredTab();
    const graphBefore = JSON.stringify(fixture.app.graph.serialize());
    const effectsBefore = fixture.sideEffectCounts();
    const requestsBefore = fixture.productionFetchBodies().length;

    fireEvent.click(
      screen.getByRole("button", { name: "Edit App Mode setup" }),
    );
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="editing_setup"]'),
      ).not.toBeNull(),
    );
    expect(screen.getByRole("textbox", { name: "Intent" })).toBeTruthy();
    expect(
      (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
    ).toBe("app-stage-intent");
    expect(JSON.stringify(fixture.app.graph.serialize())).toBe(graphBefore);
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
    expect(fixture.productionFetchBodies()).toHaveLength(requestsBefore);

    act(() => tab.destroy());
    act(() => tab.render(container));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="editing_setup"]'),
      ).not.toBeNull(),
    );
    expect(screen.getByRole("textbox", { name: "Intent" })).toBeTruthy();
    expect(JSON.stringify(fixture.app.graph.serialize())).toBe(graphBefore);
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
    expect(fixture.productionFetchBodies()).toHaveLength(requestsBefore);

    fireEvent.click(screen.getByRole("button", { name: "Cancel edit" }));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    await waitFor(() =>
      expect(
        (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
      ).toBe("edit-app-mode-setup"),
    );
    expect(JSON.stringify(fixture.app.graph.serialize())).toBe(graphBefore);
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
    expect(fixture.productionFetchBodies()).toHaveLength(requestsBefore);
  });

  it("keeps the prior Production revision truthful while setup editing is active", async () => {
    const { fixture, container } = await loadGenerationReadyEntry();
    // M17-21 renders the revision as a labelled badge instead of a bare "R1".
    const revision = () =>
      container.querySelector('[aria-label="revision 1"]')?.textContent;
    expect(revision()).toContain("1");
    const effectsBefore = fixture.sideEffectCounts();
    const requestsBefore = fixture.productionFetchBodies().length;

    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Edit App Mode setup" }),
    );
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="editing_setup"]'),
      ).not.toBeNull(),
    );
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
    expect(fixture.productionFetchBodies()).toHaveLength(requestsBefore);

    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    await waitFor(() => expect(revision()).toContain("1"));
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
    expect(fixture.productionFetchBodies()).toHaveLength(requestsBefore);
  });

  it("does not restore the prior projection after graph drift during editing", async () => {
    const { fixture, container } = await loadProjectedEntry();
    const tab = fixture.registeredTab();
    const effectsBefore = fixture.sideEffectCounts();

    fireEvent.click(
      screen.getByRole("button", { name: "Edit App Mode setup" }),
    );
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="editing_setup"]'),
      ).not.toBeNull(),
    );

    act(() => tab.destroy());
    fixture.setGraph(fixture.canonicalGraph(18));
    act(() => tab.render(container));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="interactive"]'),
      ).not.toBeNull(),
    );
    expect(
      screen.queryByRole("button", { name: "Edit App Mode setup" }),
    ).toBeNull();
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
  });

  it("reloads from only the retained opaque Production handle", async () => {
    const handle = `pw_${"p".repeat(43)}`;
    window.sessionStorage.setItem(
      "h3.context.production.workspace_handle.v1",
      handle,
    );
    const { fixture } = await loadEntry(async () => ({
      ok: true,
      status: 200,
      json: async () => productionProjectionWire(),
    }));
    await waitFor(() =>
      expect(fixture.productionFetchBodies()).toHaveLength(1),
    );
    expect(fixture.productionFetchBodies()[0]).toMatchObject({
      action: "read_projection",
      payload: { workspace_handle: handle },
    });
    expect(Object.keys(window.sessionStorage)).toEqual([
      "h3.context.production.workspace_handle.v1",
      "h3.context.provider.session_handle.v1",
      "h3.context.production.destination.v1",
    ]);
  });

  it("defers a stored Production startup read until the host workflow exists", async () => {
    const handle = `pw_${"p".repeat(43)}`;
    window.sessionStorage.setItem(
      "h3.context.production.workspace_handle.v1",
      handle,
    );
    let active: object | undefined;
    appModeStartHarness.workflow = () => active;
    const { fixture, container } = await loadEntry(async () => ({
      ok: true,
      status: 200,
      json: async () => productionProjectionWire(),
    }));
    await act(async () => Promise.resolve());
    expect(fixture.productionFetchBodies()).toHaveLength(0);
    expect(
      window.sessionStorage.getItem("h3.context.production.destination.v1"),
    ).toBeNull();
    act(() => fixture.registeredExtension().beforeConfigureGraph?.());
    active = fixture.workflowState().activeWorkflow;
    act(() => {
      fixture.registeredTab().destroy();
      fixture.registeredTab().render(container);
    });
    await waitFor(() =>
      expect(fixture.productionFetchBodies()).toHaveLength(1),
    );
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    expect(fixture.productionFetchBodies().map((body) => body.action)).toEqual([
      "read_projection",
    ]);
    expect(fixture.sideEffectCounts()).toEqual({ queues: 0, loads: 0 });
  });

  it("quarantines a stored Production read when its captured workflow changes", async () => {
    window.sessionStorage.setItem(
      "h3.context.production.workspace_handle.v1",
      `pw_${"p".repeat(43)}`,
    );
    let active: object = { owner: "initial" };
    appModeStartHarness.workflow = () => active;
    let settle!: (value: {
      ok: boolean;
      status: number;
      json(): Promise<unknown>;
    }) => void;
    const response = new Promise<{
      ok: boolean;
      status: number;
      json(): Promise<unknown>;
    }>((resolve) => {
      settle = resolve;
    });
    const { fixture } = await loadEntry(() => response);
    expect(fixture.productionFetchBodies()).toHaveLength(1);
    active = { owner: "different" };
    await act(async () => {
      settle({
        ok: true,
        status: 200,
        json: async () => productionProjectionWire(),
      });
    });
    expect(
      window.sessionStorage.getItem("h3.context.production.destination.v1"),
    ).toBeNull();
    expect(fixture.sideEffectCounts()).toEqual({ queues: 0, loads: 0 });
  });

  it("keeps stored Production recovery for a single-canvas host without a workflow store", async () => {
    window.sessionStorage.setItem(
      "h3.context.production.workspace_handle.v1",
      `pw_${"p".repeat(43)}`,
    );
    appModeStartHarness.workflow = () => undefined;
    const { fixture } = await loadEntry(
      async () => ({
        ok: true,
        status: 200,
        json: async () => productionProjectionWire(),
      }),
      undefined,
      (value) => {
        Object.defineProperty(value.app.extensionManager, "workflow", {
          value: undefined,
          configurable: true,
        });
      },
    );
    await waitFor(() =>
      expect(fixture.productionFetchBodies()).toHaveLength(1),
    );
    expect(fixture.productionFetchBodies()[0]).toMatchObject({
      action: "read_projection",
    });
    expect(fixture.sideEffectCounts()).toEqual({ queues: 0, loads: 0 });
  });

  it("release clears the opaque handle and re-admits the same Context once", async () => {
    let createCount = 0;
    const { fixture } = await loadEntry(async (_path, init) => {
      const body = JSON.parse(String(init.body)) as { action: string };
      if (body.action === "release_workspace")
        return { ok: true, status: 204, json: async () => ({}) };
      const wire = productionProjectionWire();
      if (createCount++ > 0) wire.workspace_handle = `pw_${"n".repeat(43)}`;
      return { ok: true, status: 201, json: async () => wire };
    });
    act(() => fixture.dispatchProjection("17"));
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    expect(
      window.sessionStorage.getItem(
        "h3.context.production.workspace_handle.v1",
      ),
    ).toMatch(/^pw_/);
    fireEvent.click(screen.getByRole("button", { name: "Release workspace" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm release" }));
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "Context" })
          .getAttribute("aria-current"),
      ).toBe("page"),
    );
    expect(
      window.sessionStorage.getItem(
        "h3.context.production.workspace_handle.v1",
      ),
    ).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    fireEvent.click(await screen.findByRole("button", { name: "New project" }));
    await screen.findByRole("region", { name: "Segment" });
    const actions = fixture.productionFetchBodies().map((body) => body.action);
    expect(actions).toEqual([
      "create_workspace_from_context",
      "release_workspace",
      "create_workspace_from_context",
    ]);
    expect(
      actions.filter((action) => action === "create_workspace_from_context"),
    ).toHaveLength(2);
  });

  it("ordinary unmount aborts and quarantines a late Production response", async () => {
    let resolveResponse:
      | ((value: {
          ok: boolean;
          status: number;
          json(): Promise<unknown>;
        }) => void)
      | undefined;
    const pending = new Promise<{
      ok: boolean;
      status: number;
      json(): Promise<unknown>;
    }>((resolve) => {
      resolveResponse = resolve;
    });
    const { fixture, container } = await loadEntry(() => pending);
    act(() => fixture.dispatchProjection("17"));
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByText("Production action in progress.");
    act(() => fixture.registeredTab().destroy());
    resolveResponse?.({
      ok: true,
      status: 201,
      json: async () => productionProjectionWire(),
    });
    await act(async () => Promise.resolve());
    expect(
      window.sessionStorage.getItem(
        "h3.context.production.workspace_handle.v1",
      ),
    ).toBeNull();
    act(() => fixture.registeredTab().render(container));
    expect(fixture.productionFetchBodies()).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Segment" })).toBeNull();
  });

  it("restores page-local focus without stacking page bodies", async () => {
    const { fixture } = await loadEntry(async () => ({
      ok: true,
      status: 201,
      json: async () => productionProjectionWire(),
    }));
    act(() => fixture.dispatchProjection("17"));
    await screen.findByRole("button", { name: "Production" });

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const language = screen.getByRole("combobox", { name: "Language" });
    language.focus();
    expect((document.activeElement as HTMLElement).dataset.h3FocusKey).toBe(
      "settings-language",
    );
    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "Context" })
          .getAttribute("aria-current"),
      ).toBe("page"),
    );
    expect(screen.queryByRole("combobox", { name: "Language" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await waitFor(() =>
      expect((document.activeElement as HTMLElement).dataset.h3FocusKey).toBe(
        "settings-language",
      ),
    );
    expect(screen.queryByRole("textbox", { name: /intent/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    await screen.findByRole("region", { name: "Segment" });
    screen.getByRole("button", { name: "Production" }).focus();
    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    fireEvent.click(screen.getByRole("button", { name: "Production" }));
    await waitFor(() =>
      expect((document.activeElement as HTMLElement).dataset.h3FocusKey).toBe(
        "page-production",
      ),
    );
  });

  it("reuses the exact request id after an ambiguous Production failure", async () => {
    const { fixture } = await loadEntry();
    let attempts = 0;
    fixture.setFetchApiHandler(async () => {
      attempts += 1;
      if (attempts === 1) throw new TypeError("synthetic network failure");
      return {
        ok: true,
        status: 201,
        json: async () => productionProjectionWire(),
      };
    });
    act(() => fixture.dispatchProjection("17"));
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    await screen.findByRole("alert");
    fireEvent.click(
      screen.getByRole("button", { name: "Retry Production setup" }),
    );
    await screen.findByRole("region", { name: "Segment" });

    const bodies = fixture.productionFetchBodies();
    expect(bodies).toHaveLength(2);
    expect(bodies[0]?.request_id).toBe(bodies[1]?.request_id);
    expect(bodies[0]?.action).toBe("create_workspace_from_context");
  });

  it("preserves a valid non-pending projection on a same-graph remount", async () => {
    const { fixture, container, hostSettings } = await loadEntry();
    const tab = fixture.registeredTab();
    const graphBefore = JSON.stringify(fixture.app.graph.serialize());
    const effectsBefore = fixture.sideEffectCounts();

    act(() => fixture.dispatchProjection("17"));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Prompt revision" }), {
      target: { value: "Synthetic non-pending draft" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Revision reason" }), {
      target: { value: "Synthetic non-pending reason" },
    });
    screen.getByRole("textbox", { name: "Revision reason" }).focus();

    const languageSetting = (
      fixture.registeredExtension() as {
        settings?: Array<{
          id?: unknown;
          onChange?: (value: unknown) => void;
        }>;
      }
    ).settings?.find((setting) => setting.id === "H3.Context.Language");
    expect(typeof languageSetting?.onChange).toBe("function");
    act(() => {
      languageSetting?.onChange?.("auto");
      hostSettings.setSettingValue("Comfy.Locale", "zh_CN");
      hostSettings.dispatchEvent(
        new CustomEvent("Comfy.Locale.change", {
          detail: { value: "zh_CN" },
        }),
      );
    });
    await waitFor(() =>
      expect(
        screen
          .getByRole("tab", { name: "审核／验证" })
          .getAttribute("aria-selected"),
      ).toBe("true"),
    );
    expect(
      (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
    ).toBe("workspace-reason");
    act(() => languageSetting?.onChange?.("en"));
    await waitFor(() =>
      expect(
        screen
          .getByRole("tab", { name: "Audit / Validate" })
          .getAttribute("aria-selected"),
      ).toBe("true"),
    );
    expect(
      (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
    ).toBe("workspace-reason");

    act(() => tab.destroy());
    act(() => tab.render(container));

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    expect(
      screen
        .getByRole("tab", { name: "Audit / Validate" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      (
        screen.getByRole("textbox", {
          name: "Prompt revision",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe("Synthetic non-pending draft");
    expect(
      (
        screen.getByRole("textbox", {
          name: "Revision reason",
        }) as HTMLInputElement
      ).value,
    ).toBe("Synthetic non-pending reason");
    await waitFor(() =>
      expect(
        (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
      ).toBe("workspace-reason"),
    );
    expect(
      screen
        .getByRole("button", { name: "Context" })
        .getAttribute("aria-current"),
    ).toBe("page");
    expect(JSON.stringify(fixture.app.graph.serialize())).toBe(graphBefore);
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
  });

  it("does not reuse a consumed mount focus claim on ordinary updates", async () => {
    const { fixture, container, hostSettings } = await loadEntry();
    const tab = fixture.registeredTab();
    act(() => fixture.dispatchProjection("17"));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    screen.getByRole("textbox", { name: "Revision reason" }).focus();
    act(() => tab.destroy());
    act(() => tab.render(container));
    await waitFor(() =>
      expect(
        (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
      ).toBe("workspace-reason"),
    );

    const extension = fixture.registeredExtension() as {
      settings?: Array<{
        id?: unknown;
        onChange?: (value: unknown) => void;
      }>;
      nodeCreated?: () => void;
    };
    const languageSetting = extension.settings?.find(
      (setting) => setting.id === "H3.Context.Language",
    );
    const prompt = screen.getByRole("textbox", {
      name: "Prompt revision",
    }) as HTMLTextAreaElement;
    prompt.focus();
    act(() => {
      languageSetting?.onChange?.("auto");
      hostSettings.setSettingValue("Comfy.Locale", "zh_CN");
      hostSettings.dispatchEvent(
        new CustomEvent("Comfy.Locale.change", {
          detail: { value: "zh_CN" },
        }),
      );
    });
    await waitFor(() =>
      expect(
        (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
      ).toBe("workspace-prompt"),
    );

    const contextPage = container.querySelector<HTMLElement>(
      '[data-h3-focus-key="page-context"]',
    );
    expect(contextPage).not.toBeNull();
    contextPage?.focus();
    act(() => extension.nodeCreated?.());
    await waitFor(() =>
      expect(
        (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
      ).toBe("page-context"),
    );

    expect(contextPage?.getAttribute("aria-current")).toBe("page");
  });

  it("invalidates replacement and disposal focus claims before late commit", async () => {
    const { fixture } = await loadEntry();
    expect(
      (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
    ).toBe("page-context");
    const tab = fixture.registeredTab();
    const extension = fixture.registeredExtension() as {
      h3DisposeExtension?: () => void;
    };
    const sentinel = document.createElement("button");
    const firstReplacement = document.createElement("section");
    const secondReplacement = document.createElement("section");
    document.body.append(sentinel, firstReplacement, secondReplacement);
    sentinel.focus();

    act(() => {
      tab.destroy();
      tab.render(firstReplacement);
      tab.render(secondReplacement);
      extension.h3DisposeExtension?.();
    });
    await act(async () => Promise.resolve());

    expect(document.activeElement).toBe(sentinel);
    expect(firstReplacement.childNodes).toHaveLength(0);
    expect(secondReplacement.childNodes).toHaveLength(0);
  });

  it("restores host geometry when another extension reuses the mount without destroy", async () => {
    const { fixture, container } = await loadEntry();
    const content = container.closest<HTMLElement>(
      ".sidebar-content-container",
    );
    const panel = container.closest<HTMLElement>(".side-bar-panel");
    expect(content).not.toBeNull();
    expect(panel).not.toBeNull();
    expect(container.style.minWidth).toBe("704px");
    expect(content?.style.minWidth).toBe("704px");
    expect(panel?.style.minWidth).toBe("704px");
    const ownedStyles = document.querySelectorAll<HTMLStyleElement>(
      "style[data-h3-context]",
    );
    const ownedStyle = ownedStyles.item(ownedStyles.length - 1);
    expect(ownedStyle?.isConnected).toBe(true);
    const removeEventListener = vi.spyOn(container, "removeEventListener");

    const replacement = document.createElement("section");
    replacement.dataset.foreignExtension = "";
    await act(async () => {
      container.replaceChildren(replacement);
      await Promise.resolve();
    });

    expect(container.style.cssText).toBe("");
    expect(content?.style.cssText).toBe("");
    expect(panel?.style.cssText).toBe("");
    expect(ownedStyle?.isConnected).toBe(false);
    expect(removeEventListener).toHaveBeenCalledWith(
      "focusin",
      expect.any(Function),
    );
    expect(container.childNodes).toHaveLength(1);
    expect(container.firstChild).toBe(replacement);

    act(() => fixture.registeredTab().destroy());
    expect(container.childNodes).toHaveLength(1);
    expect(container.firstChild).toBe(replacement);
  });

  it("rolls back the shared slot when width initialization fails before returning a disposer", async () => {
    const { fixture, container } = await loadEntry();
    const tab = fixture.registeredTab();
    const content = container.closest<HTMLElement>(
      ".sidebar-content-container",
    );
    const panel = container.closest<HTMLElement>(".side-bar-panel");
    act(() => tab.destroy());
    const sentinel = document.createElement("section");
    sentinel.dataset.foreignExtension = "";
    container.append(sentinel);
    const styleCount = document.querySelectorAll(
      "style[data-h3-context]",
    ).length;
    const schedule = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation(() => {
        throw new Error("synthetic schedule failure");
      });

    expect(() => act(() => tab.render(container))).toThrow(
      /synthetic schedule failure/,
    );
    expect(container.style.cssText).toBe("");
    expect(content?.style.cssText).toBe("");
    expect(panel?.style.cssText).toBe("");
    expect(document.querySelectorAll("style[data-h3-context]")).toHaveLength(
      styleCount,
    );
    expect(container.childNodes).toHaveLength(1);
    expect(container.firstChild).toBe(sentinel);

    schedule.mockRestore();
    act(() => tab.render(container));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="interactive"]'),
      ).not.toBeNull(),
    );
    act(() => tab.destroy());
    expect(container.childNodes).toHaveLength(0);
  });

  it("fails closed when a non-pending projection graph changes while closed", async () => {
    const { fixture, container } = await loadEntry();
    const tab = fixture.registeredTab();
    const effectsBefore = fixture.sideEffectCounts();
    act(() => fixture.dispatchProjection("17"));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );

    act(() => tab.destroy());
    fixture.setGraph(fixture.canonicalGraph(18));
    act(() => tab.render(container));

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="interactive"]'),
      ).not.toBeNull(),
    );
    expect(
      container.querySelector('[data-h3-focus-key="workspace-stage-audit"]'),
    ).toBeNull();
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
  });

  it("does not fabricate non-pending provenance when entry serialization fails", async () => {
    const { fixture, container } = await loadEntry();
    const tab = fixture.registeredTab();
    const graph = fixture.app.graph.serialize();
    const effectsBefore = fixture.sideEffectCounts();
    let serializedReads = 0;
    const serialize = vi
      .spyOn(fixture.app.graph, "serialize")
      .mockImplementation(() => {
        serializedReads += 1;
        if (serializedReads === 2)
          throw new Error("synthetic acceptance provenance failure");
        return structuredClone(graph);
      });

    act(() => fixture.dispatchProjection("17"));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    expect(serializedReads).toBe(2);
    serialize.mockImplementation(() => structuredClone(graph));

    act(() => tab.destroy());
    act(() => tab.render(container));

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="interactive"]'),
      ).not.toBeNull(),
    );
    expect(
      container.querySelector('[data-h3-focus-key="workspace-stage-audit"]'),
    ).toBeNull();
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
  });

  it("fails closed when a non-pending projection graph becomes unreadable", async () => {
    const { fixture, container } = await loadEntry();
    const tab = fixture.registeredTab();
    const effectsBefore = fixture.sideEffectCounts();
    act(() => fixture.dispatchProjection("17"));
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );

    act(() => tab.destroy());
    vi.spyOn(fixture.app.graph, "serialize").mockImplementation(() => {
      throw new Error("synthetic unreadable graph");
    });
    act(() => tab.render(container));

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="interactive"]'),
      ).not.toBeNull(),
    );
    expect(
      container.querySelector('[data-h3-focus-key="workspace-stage-audit"]'),
    ).toBeNull();
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
  });

  it("preserves the exact projected draft on a same-graph remount refresh", async () => {
    const { fixture, container } = await loadProjectedEntry();
    const tab = fixture.registeredTab();
    const graphBefore = JSON.stringify(fixture.app.graph.serialize());
    const effectsBefore = fixture.sideEffectCounts();

    act(() => tab.destroy());
    act(() => tab.render(container));

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    expect(
      screen
        .getByRole("tab", { name: "Audit / Validate" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      (
        screen.getByRole("textbox", {
          name: "Prompt revision",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe("Synthetic projected draft");
    expect(
      (
        screen.getByRole("textbox", {
          name: "Revision reason",
        }) as HTMLInputElement
      ).value,
    ).toBe("Synthetic projected reason");
    await waitFor(() =>
      expect(
        (document.activeElement as HTMLElement | null)?.dataset.h3FocusKey,
      ).toBe("workspace-reason"),
    );
    expect(
      screen
        .getByRole("button", { name: "Context" })
        .getAttribute("aria-current"),
    ).toBe("page");
    expect(JSON.stringify(fixture.app.graph.serialize())).toBe(graphBefore);
    expect(fixture.sideEffectCounts()).toEqual(effectsBefore);
  });

  it("fails closed when the graph changes while the projected view is closed", async () => {
    const { fixture, container } = await loadProjectedEntry();
    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    fixture.setGraph(fixture.canonicalGraph(18));
    act(() => tab.render(container));

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="interactive"]'),
      ).not.toBeNull(),
    );
    expect(
      container.querySelector('[data-h3-focus-key="workspace-stage-audit"]'),
    ).toBeNull();
  });
});

describe("entry generation admission lifecycle", () => {
  it("keeps a real refusal and states it on the App Mode form", async () => {
    appModeAdmissionHarness.answers = [
      {
        status: "refused",
        reason: "unsupported_host",
        remediation: "upgrade_host",
        unsatisfiedSlots: [],
      } as never,
    ];
    await loadEntry(undefined, { nodes: [] });
    const start = await screen.findByRole("button", {
      name: /H3 App Mode$/,
    });
    await waitFor(() =>
      expect((start as HTMLButtonElement).disabled).toBe(true),
    );
    expect(
      document
        .querySelector("#h3-app-mode-generation-blocker")
        ?.getAttribute("data-h3-generation-blocker"),
    ).toBe("unsupported_host");
    // A decision is a decision: the shell does not go back and ask again.
    fireEvent.change(screen.getByRole("textbox", { name: "Intent" }), {
      target: { value: "A different bounded intent." },
    });
    expect(appModeAdmissionHarness.calls).toBe(1);
  });

  it("asks again after a capability read that could not be answered", async () => {
    // `profile_unavailable` is a host that could not be asked, not a decision.
    // Caching it would leave the sidebar refusing for the rest of the session
    // over one unreachable read.
    appModeAdmissionHarness.answers = [
      {
        status: "refused",
        reason: "profile_unavailable",
        remediation: "upgrade_host",
        unsatisfiedSlots: [],
      } as never,
    ];
    await loadEntry(undefined, { nodes: [] });
    await screen.findByRole("button", { name: /H3 App Mode$/ });
    await waitFor(() => expect(appModeAdmissionHarness.calls).toBe(1));
    expect(
      document.querySelector("#h3-app-mode-generation-blocker"),
    ).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "Intent" }), {
      target: { value: "A different bounded intent." },
    });
    await waitFor(() => expect(appModeAdmissionHarness.calls).toBe(2));
    expect(
      document.querySelector("#h3-app-mode-generation-blocker"),
    ).toBeNull();
  });

  it("keeps a relocated materialize notice out of the existing working transition", async () => {
    const deferred = deferredAppModeStart();
    let fixture: EntryHostFixture | undefined;
    let observedOptions: AppModeStartOptions | undefined;
    appModeAdmissionHarness.answers = [
      {
        status: "refused",
        reason: "asset_relocated",
        remediation: "select_installed_asset_on_canvas",
        unsatisfiedSlots: ["video_unet", "text_encoder"],
      } as never,
    ];
    appModeStartHarness.start = async (_inputs, options = {}) => {
      observedOptions = options;
      return deferred.promise;
    };
    const loaded = await loadEntry(
      undefined,
      compatibleExistingGraph() as Record<string, unknown>,
    );
    fixture = loaded.fixture;
    await waitFor(() => expect(appModeAdmissionHarness.calls).toBe(1));

    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        loaded.container.querySelector('[data-app-mode-phase="compiling"]'),
      ).not.toBeNull(),
    );
    expect(observedOptions?.useExisting).toBe(true);
    const noticeDuringStart = loaded.container.querySelector(
      "#h3-app-mode-generation-blocker",
    );
    const scopeDuringStart = loaded.container
      .querySelector(".h3-app-mode-actions")
      ?.getAttribute("data-h3-action-scope");

    act(() =>
      deferred.resolve({
        queueResult: acceptedQueueReceipt("prompt-existing-working"),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(
          fixture!.app.graph.serialize() as unknown,
        ),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: "prompt-existing-working",
        route: "existing",
      }),
    );
    await waitFor(() =>
      expect(
        loaded.container.querySelector('[data-app-mode-phase="queueing"]'),
      ).not.toBeNull(),
    );
    expect(noticeDuringStart).toBeNull();
    expect(scopeDuringStart).toBeNull();
    expect(
      loaded.container.querySelector("#h3-app-mode-generation-blocker"),
    ).toBeNull();
  });

  it("keeps a relocated materialize notice out of the Connect working transition", async () => {
    const deferred = deferredAppModeStart();
    let fixture: EntryHostFixture | undefined;
    let observedOptions: AppModeStartOptions | undefined;
    appModeAdmissionHarness.answers = [
      {
        status: "refused",
        reason: "asset_relocated",
        remediation: "select_installed_asset_on_canvas",
        unsatisfiedSlots: ["video_unet", "text_encoder"],
      } as never,
    ];
    appModeConnectHarness.candidates = {
      tier: "connect",
      anchors: [
        {
          nodeId: 6,
          anchorType: "MiniMaxH3ImageToVideo",
          nested: false,
          taskMode: "t2va",
        },
      ],
    };
    appModeStartHarness.start = async (_inputs, options = {}) => {
      observedOptions = options;
      return deferred.promise;
    };
    const loaded = await loadEntry(
      undefined,
      loadSyntheticTemplate("video_minimax_h3_t2v") as Record<string, unknown>,
    );
    fixture = loaded.fixture;
    const connectButton = await screen.findByRole("button", {
      name: "Connect and queue current canvas",
    });
    expect(
      loaded.container.querySelector("#h3-app-mode-generation-blocker"),
    ).toBeNull();
    expect(
      loaded.container
        .querySelector(".h3-app-mode-actions")
        ?.getAttribute("data-h3-action-scope"),
    ).toBeNull();

    fireEvent.click(connectButton);
    await waitFor(() =>
      expect(
        loaded.container.querySelector('[data-app-mode-phase="materializing"]'),
      ).not.toBeNull(),
    );
    expect(observedOptions?.connectExisting).toEqual({ anchorNodeId: 6 });
    const noticeDuringStart = loaded.container.querySelector(
      "#h3-app-mode-generation-blocker",
    );
    const scopeDuringStart = loaded.container
      .querySelector(".h3-app-mode-actions")
      ?.getAttribute("data-h3-action-scope");

    act(() =>
      deferred.resolve({
        queueResult: acceptedQueueReceipt("prompt-connect-working"),
        ...OWNED_RESULT_IDENTITY,
        graphFingerprint: appModeFingerprint(
          fixture!.app.graph.serialize() as unknown,
        ),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: "prompt-connect-working",
        route: "connect",
      }),
    );
    await waitFor(() =>
      expect(
        loaded.container.querySelector('[data-app-mode-phase="queueing"]'),
      ).not.toBeNull(),
    );
    expect(noticeDuringStart).toBeNull();
    expect(scopeDuringStart).toBeNull();
    expect(
      loaded.container.querySelector("#h3-app-mode-generation-blocker"),
    ).toBeNull();
  });
});

describe("entry graph configure ownership", () => {
  /**
   * The run writes the canvas, so the host announces a configure. The hook that
   * hears it exists to abandon a run when the *user* replaces the canvas, and
   * before the write was declared it abandoned every materialization instead --
   * on a real host, at the exact moment the graph landed.
   */
  it("survives the canvas write the run performs itself", async () => {
    let fixture: EntryHostFixture | undefined;
    let declaredDuringWrite = false;
    appModeStartHarness.start = async (_inputs, options = {}) => {
      const release = ownedConfigureHarness.begin?.();
      declaredDuringWrite = release !== undefined;
      try {
        (
          fixture?.registeredExtension() as {
            beforeConfigureGraph?: () => void;
          }
        ).beforeConfigureGraph?.();
      } finally {
        release?.();
      }
      return {
        queueResult: acceptedQueueReceipt("prompt-1"),
        ...OWNED_RESULT_IDENTITY,
        transactionId: options.transactionId,
        graphFingerprint: appModeFingerprint(
          fixture!.app.graph.serialize() as unknown,
        ),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: "prompt-1",
        route: "replace" as const,
      };
    };
    const loaded = await loadEntry();
    fixture = loaded.fixture;

    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        loaded.container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );
    act(() => fixture!.dispatchProjection("17"));
    await waitFor(() =>
      expect(
        loaded.container.querySelector('[data-shell-status="projected"]'),
      ).not.toBeNull(),
    );
    expect(declaredDuringWrite).toBe(true);
  });

  /**
   * M23-25 admission runs against the detached candidate. A refusal must retain
   * the error and the pre-run canvas; it cannot surface an apply-current action
   * for a candidate that was never installed.
   */
  it("keeps a structural template refusal off-canvas without offering the absent candidate", async () => {
    let fixture: EntryHostFixture | undefined;
    appModeStartHarness.prepare = async () => {
      throw new AppModeError(
        "incompatible_seam",
        "this host has no supported template splice capability",
        {
          kind: "generation_admission_refused",
          admissionReason: "template_drift",
          unsatisfiedSlots: [],
        },
      );
    };
    const loaded = await loadEntry(undefined, { nodes: [] });
    fixture = loaded.fixture;
    const beforeGraph = fixture.app.graph.serialize();

    await clickReadyAppModeStart();
    await screen.findByText(
      /generation template this version does not support/i,
    );
    expect(fixture.app.graph.serialize()).toEqual(beforeGraph);
    expect(
      screen.queryByRole("button", {
        name: "Apply and queue current H3 graph",
      }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Continue with native nodes" }),
    ).not.toBeNull();
    await waitFor(() =>
      expect(loaded.container.dataset.h3RunState).not.toBe("working"),
    );
  });

  it("still abandons a run when the configure is not its own", async () => {
    let fixture: EntryHostFixture | undefined;
    appModeStartHarness.start = async (_inputs, options = {}) => {
      (
        fixture?.registeredExtension() as {
          beforeConfigureGraph?: () => void;
        }
      ).beforeConfigureGraph?.();
      return {
        queueResult: acceptedQueueReceipt("prompt-1"),
        ...OWNED_RESULT_IDENTITY,
        transactionId: options.transactionId,
        graphFingerprint: appModeFingerprint(
          fixture!.app.graph.serialize() as unknown,
        ),
        compiledPromptFingerprint: `sha256:${"e".repeat(64)}`,
        queuePromptId: "prompt-1",
        route: "replace" as const,
      };
    };
    const loaded = await loadEntry();
    fixture = loaded.fixture;

    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        loaded.container.querySelector('[data-shell-reason="cancelled"]'),
      ).not.toBeNull(),
    );
    act(() => fixture!.dispatchProjection("17"));
    // A projection from a transaction the user replaced must not reach the UI.
    expect(
      loaded.container.querySelector('[data-shell-status="projected"]'),
    ).toBeNull();
  });
});

describe("entry host availability and reconnect reconciliation", () => {
  it("presents a dropped socket as a host condition, not an App Mode error", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() => fixture.dispatchHostSocketDropped());

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="host_unavailable"]'),
      ).not.toBeNull(),
    );
    expect(container.querySelector('[data-shell-status="error"]')).toBeNull();
    const panel = container.querySelector<HTMLElement>(
      '[data-shell-status="host_unavailable"]',
    );
    expect(panel?.dataset.hostAvailability).toBe("lost");
    expect(panel?.dataset.shellInterrupted).toBe("working");
    expect(
      screen.queryByRole("button", { name: "Retry H3 App Mode" }),
    ).toBeNull();
    // AC-2: the interruption exposes no action that could reach the queue.
    const submit = container.querySelector<HTMLButtonElement>(
      '[data-h3-focus-key="app-submit"]',
    );
    expect(submit).not.toBeNull();
    expect(submit?.disabled).toBe(true);
  });

  it("advances to reconnecting without losing the interrupted state", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() => fixture.dispatchHostSocketDropped());
    act(() => fixture.dispatchHostReconnecting());

    await waitFor(() =>
      expect(
        container.querySelector<HTMLElement>(
          '[data-shell-status="host_unavailable"]',
        )?.dataset.hostAvailability,
      ).toBe("reconnecting"),
    );
    expect(
      container.querySelector<HTMLElement>(
        '[data-shell-status="host_unavailable"]',
      )?.dataset.shellInterrupted,
    ).toBe("working");
  });

  it("restores exactly the interrupted state when no run needs reconciling", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() => fixture.dispatchHostSocketDropped());
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="host_unavailable"]'),
      ).not.toBeNull(),
    );
    const queuesBefore = fixture.sideEffectCounts().queues;

    act(() => fixture.dispatchHostReconnected());

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );
    // AC-1: a reconnect never queues. Reconciliation only reads.
    expect(fixture.sideEffectCounts().queues).toBe(queuesBefore);
  });

  it("keeps an ordinary status payload from being read as a lost host", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() =>
      fixture.dispatchHostStatus({ exec_info: { queue_remaining: 2 } }),
    );

    expect(
      container.querySelector('[data-shell-status="host_unavailable"]'),
    ).toBeNull();
    expect(
      container.querySelector('[data-shell-status="working"]'),
    ).not.toBeNull();
  });

  it("accepts a late owning terminal that arrives after the socket returns", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() => fixture.dispatchHostSocketDropped());
    act(() => fixture.dispatchHostReconnected());
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() => fixture.dispatchExecutionTerminal("interrupted", "prompt-1"));

    await screen.findByText("The H3 execution was interrupted on the host.");
    expect(
      container.querySelector('[data-shell-status="error"]'),
    ).not.toBeNull();
  });

  it("still rejects a foreign terminal after a reconnect", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() => fixture.dispatchHostSocketDropped());
    act(() => fixture.dispatchHostReconnected());
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() => fixture.dispatchExecutionTerminal("error", "foreign-prompt"));

    expect(
      container.querySelector('[data-shell-status="working"]'),
    ).not.toBeNull();
    expect(container.querySelector('[data-shell-status="error"]')).toBeNull();
  });
});

describe("entry host interruption and canvas replacement", () => {
  it("abandons the interrupted run when the user replaces the canvas during a drop", async () => {
    const { fixture, container } = await loadEntry();
    await clickReadyAppModeStart();
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="working"]'),
      ).not.toBeNull(),
    );

    act(() => fixture.dispatchHostSocketDropped());
    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-status="host_unavailable"]'),
      ).not.toBeNull(),
    );

    // The user loads another workflow while the host is still gone. The interrupted run is
    // abandoned, and it must say so rather than resetting silently to a fresh panel.
    act(() =>
      (
        fixture.registeredExtension() as {
          beforeConfigureGraph?: () => void;
        }
      ).beforeConfigureGraph?.(),
    );

    await waitFor(() =>
      expect(
        container.querySelector('[data-shell-reason="cancelled"]'),
      ).not.toBeNull(),
    );

    // The quarantine armed with it: a late projection for the abandoned run must not land on the
    // canvas the user just replaced.
    act(() => fixture.dispatchProjection("17"));
    expect(
      container.querySelector('[data-shell-status="projected"]'),
    ).toBeNull();
  });
});

// M25-21 section 14.4 through the registered extension: the host's own tab destroy/render is the
// view release, and whole-extension disposal is the only reset. Remount may re-read currentness;
// it never re-sends an edit, Start, generation, queue or provider action.
describe("M25-21 Sidebar retention across the real view lifecycle", () => {
  const productionActions = (fixture: EntryHostFixture): string[] =>
    fixture.productionFetchBodies().map((body) => String(body.action));
  const relation = () =>
    screen.getByRole("combobox", {
      name: "Relationship for segment 1",
    }) as HTMLSelectElement;
  const openDetails = (element: HTMLDetailsElement): void => {
    element.open = true;
    fireEvent(element, new Event("toggle"));
  };

  it("restores the Production draft, expansion and function after tab destroy without sending anything", async () => {
    const { fixture, container } = await loadGenerationReadyEntry();
    fireEvent.change(relation(), { target: { value: "adjacent_pair" } });
    openDetails(container.querySelector<HTMLDetailsElement>("details.h3p-au")!);
    fireEvent.click(screen.getByRole("tab", { name: "Clip editor" }));
    const effects = fixture.sideEffectCounts();
    const sent = productionActions(fixture).length;
    const queued = fixture.queuedPrompts().length;
    const graph = JSON.stringify(fixture.app.graph.serialize());

    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    act(() => tab.render(container));
    expect(
      (await screen.findByRole("tab", { name: "Clip editor" })).getAttribute(
        "aria-selected",
      ),
    ).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: "Production" }));
    await waitFor(() => expect(relation().value).toBe("adjacent_pair"));
    expect(
      container.querySelector<HTMLDetailsElement>("details.h3p-au")!.open,
    ).toBe(true);

    expect(
      productionActions(fixture)
        .slice(sent)
        .filter((action) => action !== "read_projection"),
    ).toEqual([]);
    expect(fixture.sideEffectCounts()).toEqual(effects);
    expect(fixture.queuedPrompts()).toHaveLength(queued);
    expect(JSON.stringify(fixture.app.graph.serialize())).toBe(graph);
  });

  it("keeps Settings expansion across tab destroy but never the credential", async () => {
    const intents: string[] = [];
    const { fixture, container } = await loadEntry(async (path, init) => {
      if (path !== "/h3-context/v1/provider/settings")
        return { ok: false, status: 503, json: async () => ({}) };
      if (init.method === "DELETE")
        return { ok: true, status: 204, json: async () => ({}) };
      intents.push(
        (JSON.parse(String(init.body)) as { intent: string }).intent,
      );
      return {
        ok: true,
        status: 200,
        json: async () => ({
          schema: PROVIDER_SETTINGS_SCHEMA,
          accepted: true,
          rejection: null,
          projection: providerProjectionWire(false),
        }),
      };
    });
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    await typeCredential("test-credential-retention");
    openDetails(
      container.querySelector<HTMLDetailsElement>("details.h3s-pv-identity")!,
    );
    const read = intents.length;

    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    act(() => tab.render(container));
    const field = (await screen.findByLabelText("API key")) as HTMLInputElement;
    expect(field.value).toBe("");
    expect(
      container.querySelector<HTMLDetailsElement>("details.h3s-pv-identity")!
        .open,
    ).toBe(true);
    expect(document.body.innerHTML).not.toContain("test-credential-retention");
    await waitFor(() => expect(intents.length).toBeGreaterThan(read));
    expect(intents.slice(read)).toEqual(["read_projection"]);
  });

  it("clears the entry's session store only on whole-extension disposal", async () => {
    const { fixture, container } = await loadEntry();
    const store = retentionHarness.stores.at(-1)!;
    fireEvent.click(await screen.findByRole("button", { name: "Production" }));
    fireEvent.click(await screen.findByRole("tab", { name: "Clip editor" }));
    const retained = store.size();
    expect(retained).toBeGreaterThan(0);

    const tab = fixture.registeredTab();
    act(() => tab.destroy());
    expect(store.size()).toBe(retained);
    act(() => tab.render(container));
    expect(
      (await screen.findByRole("tab", { name: "Clip editor" })).getAttribute(
        "aria-selected",
      ),
    ).toBe("true");

    const generation = store.generation();
    act(() => fixture.registeredExtension().h3DisposeExtension?.());
    expect(store.size()).toBe(0);
    expect(store.generation()).toBe(generation + 1);
  });
});
