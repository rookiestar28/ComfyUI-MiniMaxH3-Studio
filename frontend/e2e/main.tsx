import { useCallback, useReducer, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

import {
  H3Sidebar,
  initialAppModeDraft,
  type AppModeDraft,
  type AppModeDurationResolutionState,
  type SidebarNleBinding,
} from "../src/components/H3Sidebar";
import type { NleWorkspaceBinding } from "../src/components/nle/nleWorkspaceBinding";
import {
  initialNleImportState,
  initialNlePlanningState,
  initialNleWorkspaceState,
} from "../src/state/nleWorkspaceState";
import type { WorkspaceActionRequest } from "../src/components/SidebarStages";
import type { AssistedPromptProposalProjection } from "../src/contracts/assistedPromptProposalCodec";
import type { SidebarWorkspaceProjection } from "../src/contracts/sidebarWorkspaceCodec";
import type {
  AuthoringIntent,
  AuthoringViewState,
} from "../src/state/authoringViewState";
import type {
  ProductionIntent,
  ProductionViewState,
} from "../src/components/ProductionWorkbench";
import {
  decodeAuthoringProjection,
  decodeTimelineHistoryProjection,
  decodeTimelineReceipt,
  type TimelineCommandWire,
  type TimelineHistoryProjection,
  type TimelineReceipt,
} from "../src/contracts/authoringWorkbenchCodec";
import { decodeProductionWorkbenchProjection } from "../src/contracts/productionWorkbenchCodec";
import type { ProductShellProjection } from "../src/contracts/projectionCodecs";
import {
  decodeTransactionTransparencyProjection,
  type TransactionIntent,
} from "../src/contracts/transactionTransparencyCodec";
import {
  decodeSemanticProposalActionResult,
  decodeSemanticProposalReviewHandle,
  decodeSemanticProposalReviewProjection,
  type SemanticProposalReviewProjection,
} from "../src/contracts/semanticProposalReviewCodec";
import type { Locale } from "../src/i18n/catalog";
import type { LocalePreference } from "../src/i18n/localeStore";
import {
  PROVIDER_SETTINGS_SCHEMA,
  type DiscoveryCandidateView,
  type ModelMetadataView,
  type ProviderIntent,
  type ProviderIntentPayload,
  type ProviderSettingsProjection,
} from "../src/contracts/providerSettingsCodec";
import type { ProductionProposalRow } from "../src/host/productionProposalDispatcher";
import type { ProductionMediaPreviewState } from "../src/host/productionMediaPreview";
import type { AuthoringPreviewOpener } from "../src/host/authoringFrameCoordinator";
import type { PageId } from "../src/navigation/pageRegistry";
import {
  reduceSemanticProposalReviewState,
  type SemanticProposalMutation,
} from "../src/state/semanticProposalReview";
import {
  initialShellState,
  reduceShellState,
  retainedProjectionOwnsPrompt,
  type ShellState,
} from "../src/state/shellState";
import type { SidebarWorkspaceState } from "../src/state/sidebarWorkspace";
import {
  validProductShell,
  validSidebarWorkspace,
} from "../tests/sidebarWorkspaceFixture";
import compositionFixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import { unavailableProductionAssemblyWire } from "../tests/support/productionAssemblyWire";
import "../src/styles/tokens.css";

const fp = (character: string) => `sha256:${character.repeat(64)}`;
const HISTORY_CURSOR_A = `h3.context.timeline_history_cursor.v1:1:${"a".repeat(64)}`;
const HISTORY_CURSOR_B = `h3.context.timeline_history_cursor.v1:2:${"b".repeat(64)}`;
const prepared = {
  schema: "h3.context.transaction_transparency.v1",
  workspace_id: "workspace.1",
  workspace_revision: 2,
  workspace_fingerprint: fp("a"),
  recompute_plan_fingerprint: fp("b"),
  correlation: { prompt_id: "prompt.1", execution_node_id: "17" },
  selection_safe: true,
  requires_full_recompute: false,
  mandatory_segment_ids: ["segment.1", "segment.2"],
  requested_segment_ids: ["segment.1", "segment.2"],
  missing_required_segment_ids: [],
  decisions: [
    {
      segment_id: "segment.1",
      disposition: "dirty_self",
      reason_codes: ["producer_fingerprint_changed"],
      triggering_segment_ids: ["segment.1"],
    },
    {
      segment_id: "segment.2",
      disposition: "dirty_upstream",
      reason_codes: ["upstream_producer_changed"],
      triggering_segment_ids: ["segment.1"],
    },
  ],
  transaction: {
    transaction_id: "transaction.1",
    transaction_fingerprint: fp("c"),
    attempt: 1,
    state: "prepared",
    graph_fingerprint: fp("d"),
    compiled_prompt_fingerprint: fp("e"),
    queue_prompt_id: null,
    host_owner_id: null,
    result_fingerprint: null,
    cancellation_requested: false,
  },
  actions: {
    confirm_native_queue: true,
    inspect_queue_history: false,
    return_to_native: true,
    rerun: false,
  },
  guidance: "review_before_native_queue",
} as const;

const unknown = {
  ...prepared,
  transaction: {
    ...prepared.transaction,
    state: "unknown_ownership",
    queue_prompt_id: "prompt.1",
  },
  actions: {
    confirm_native_queue: false,
    inspect_queue_history: true,
    return_to_native: true,
    rerun: false,
  },
  guidance: "ownership_unknown",
} as const;

const reviewHandle = decodeSemanticProposalReviewHandle({
  schema: "h3.context.semantic_proposal_review_handle.v1",
  review_id: `review_${"r".repeat(32)}`,
  transaction_fingerprint: fp("f"),
  workspace_fingerprint: fp("1"),
  report_fingerprint: fp("2"),
  correlation: { prompt_id: "prompt.1", execution_node_id: "17" },
  available: true,
  reason: "review_available",
});
const initialReview = decodeSemanticProposalReviewProjection({
  schema: "h3.context.semantic_proposal_review.v1",
  review_id: reviewHandle.review_id,
  transaction_fingerprint: reviewHandle.transaction_fingerprint,
  workspace_id: "workspace.review",
  workspace_revision: 1,
  workspace_fingerprint: reviewHandle.workspace_fingerprint,
  report_fingerprint: reviewHandle.report_fingerprint,
  attempt: 1,
  revision: 1,
  state: "ready_for_review",
  segment_id: "segment.review",
  correlation: reviewHandle.correlation,
  changed_collections: ["scenes"],
  groups: [
    {
      collection: "scenes",
      items: [
        {
          target_id: "scene.review",
          summary: "Synthetic morning scene",
          change_kind: "modified",
          reference_labels: ["<Picture 1>"],
          constraint_labels: ["scene:scene.review"],
          uncertainty_codes: [],
          reason_code: "candidate_modified",
        },
      ],
    },
  ],
  clarifications: [],
  uncertainty_codes: [],
  reason_code: "proposal_ready",
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
});
const clarificationReview = decodeSemanticProposalReviewProjection({
  ...initialReview,
  state: "clarification_required",
  clarifications: [
    {
      clarification_id: "clarify.scene",
      label: "Confirm synthetic scene continuity",
      reason_code: "resolution_required",
    },
  ],
  uncertainty_codes: ["clarification_required"],
  reason_code: "clarification_required",
  actions: {
    ...initialReview.actions,
    proposal_resolve: true,
    proposal_accept: false,
  },
});
const acceptedReview = decodeSemanticProposalReviewProjection({
  ...initialReview,
  revision: 2,
  state: "accepted",
  actions: {
    ...initialReview.actions,
    proposal_accept: false,
    proposal_reject: false,
    proposal_cancel: false,
  },
  terminal: "accepted",
});
const successorReviewHandle = decodeSemanticProposalReviewHandle({
  ...reviewHandle,
  review_id: `review_${"s".repeat(32)}`,
  transaction_fingerprint: fp("5"),
  workspace_fingerprint: fp("6"),
});

// M17-23 Guard A needs a card carrying a fault tone, which the default states
// never produce. The flag decorates the second segment only, so every existing
// row that counts or tones cards is untouched.
// M17-25. Harness segments carry producible lengths, because a segment can no
// longer represent anything else. The table is written out rather than computed
// so the harness cannot become a second copy of the alignment authority, and it
// deliberately alternates exact and snapped requests: the delivered-duration
// readout and the snapped marker both need something to show.
const PRODUCIBLE_DURATIONS = [
  {
    duration_milliseconds: 5167,
    delivered_milliseconds: 5167,
    frame_count: 124,
    snapped: false,
  },
  {
    duration_milliseconds: 4167,
    delivered_milliseconds: 4458,
    frame_count: 107,
    snapped: true,
  },
  {
    duration_milliseconds: 8000,
    delivered_milliseconds: 8000,
    frame_count: 192,
    snapped: false,
  },
  {
    duration_milliseconds: 12500,
    delivered_milliseconds: 12958,
    frame_count: 311,
    snapped: true,
  },
] as const;

function producibleDuration(index: number) {
  return { ...PRODUCIBLE_DURATIONS[index % PRODUCIBLE_DURATIONS.length]! };
}

const TONED_SEGMENT_STATES = {
  closure_state: "requires_full_recompute",
  job_state: "failed",
  artifact_state: "failed",
} as const;

function productionProjection(
  revision: number,
  segmentIds: readonly string[],
  selectedIds: readonly string[],
  outputCount = 0,
  toned = false,
  authorityRevision?: number,
  previewSegmentIds: readonly string[] = [],
) {
  const character = String(revision % 10);
  const hasAuthority = authorityRevision !== undefined;
  // IMPORTANT: outputCount never implies generation authority. Aggregate-output fixtures must
  // pass the matching authority revision or they model an impossible reconstruction state.
  // IMPORTANT: keep eligibility independent per output row; an aggregate-only
  // fixture hid the clip-preview journey while every layer-level test stayed green.
  const previewableSegments = new Set(previewSegmentIds);
  return decodeProductionWorkbenchProjection({
    schema: "h3.context.production_workbench.projection.v1",
    workspace_handle: `pw_${"p".repeat(43)}`,
    workspace_id: "workspace.production",
    workspace_revision: revision,
    workspace_fingerprint: fp(character),
    segments: segmentIds.map((segmentId, index) => ({
      segment_id: segmentId,
      ordinal: index + 1,
      task_mode: "t2va",
      duration: producibleDuration(index),
      relation: "independent",
      predecessor_segment_id: null,
      boundary_kind: "independent",
      closure_state: hasAuthority ? "clean" : "unavailable",
      job_state: hasAuthority ? "succeeded" : "unavailable",
      artifact_state: hasAuthority ? "complete" : "unavailable",
      continuity_state: "unavailable",
      delivered_geometry: hasAuthority
        ? {
            format: "mp4",
            frame_count: producibleDuration(index).frame_count,
            width: 768,
            height: 512,
          }
        : null,
      ...(toned && index === 1 ? TONED_SEGMENT_STATES : {}),
    })),
    selected_segment_ids: selectedIds,
    run: hasAuthority
      ? {
          state: "succeeded",
          completed: segmentIds.length,
          total: segmentIds.length,
        }
      : { state: "unavailable", completed: 0, total: 0 },
    generation_sequence: hasAuthority
      ? {
          schema: "h3.context.generation_sequence_projection.v1",
          sequence_id: "sequence.production.completed",
          sequence_fingerprint: fp("8"),
          state_fingerprint: fp("9"),
          workspace_id: "workspace.production",
          workspace_revision: authorityRevision,
          workspace_fingerprint: fp(String(authorityRevision % 10)),
          correlation: {
            prompt_id: "prompt.production.completed",
            execution_node_id: "node.production.completed",
          },
        }
      : null,
    reconstruction: { state: hasAuthority ? "complete" : "unavailable" },
    // IMPORTANT: keep the hermetic harness on the same closed assembly wire as unit fixtures;
    // omitting a newly required field aborts Production before any browser journey can mount.
    assembly: unavailableProductionAssemblyWire(),
    authority_versions: hasAuthority
      ? [
          "h3.context.generation_sequence_projection.v1",
          "h3.context.segment_artifact_receipt.v1",
          "h3.context.av_reconstruction_receipt.v1",
        ]
      : [],
    outputs: Array.from({ length: outputCount }, (_, index) => ({
      output_handle: `out_${String(index).padStart(16, "0")}`,
      ordinal: index + 1,
      state: "ready",
      segment_id: index === 0 ? null : (segmentIds[index - 1] ?? null),
      preview:
        index === 0 ||
        (index > 0 && previewableSegments.has(segmentIds[index - 1] ?? "")),
    })),
    allowed_actions: [
      ...(segmentIds.length < 64 ? ["add_segment_from_context"] : []),
      "replace_segment_from_context",
      "set_segment_relation",
      ...(segmentIds.length > 1 ? ["delete_segment", "reorder_segments"] : []),
      "set_selection",
      "read_projection",
      "release_workspace",
      ...(outputCount > 0 ? ["preview_output"] : []),
    ],
    blocker_codes: hasAuthority ? [] : ["sequence_authority_unavailable"],
    limits: { max_segments: 64, max_outputs: 65 },
  });
}

type AuthoringHarnessClip = {
  clip_id: string;
  asset_id: string;
  kind: "video" | "audio";
  lane: number;
  start_frame: number;
  frames: number;
  source_start_frame: number;
};

type AuthoringHarnessModel = {
  referenceRevision: number;
  timelineRevision: number;
  nextClipNumber: number;
  clips: AuthoringHarnessClip[];
  links: { video_clip_id: string; audio_clip_id: string }[];
  selection: string[];
  soundtrackExcluded: boolean;
  availabilityProducer: string | null;
  availabilityRevision: number;
};

function initialAuthoringModel(): AuthoringHarnessModel {
  return {
    referenceRevision: 1,
    timelineRevision: 1,
    nextClipNumber: 2,
    clips: [
      {
        clip_id: "clip-1",
        asset_id: "vid-1",
        kind: "video",
        lane: 0,
        start_frame: 0,
        frames: 24,
        source_start_frame: 0,
      },
    ],
    links: [],
    selection: [],
    soundtrackExcluded: false,
    availabilityProducer: null,
    availabilityRevision: 0,
  };
}

function authoringHistory(
  selection: readonly string[] = [],
  undoCursor: string | null = null,
  redoCursor: string | null = null,
  rejection: string | null = null,
): TimelineHistoryProjection {
  return decodeTimelineHistoryProjection({
    schema: "h3.context.timeline_history_projection.v1",
    workspace_handle: compositionFixture.snapshot.workspace_handle,
    snapshot: compositionFixture.snapshot,
    selection,
    undo_cursor: undoCursor,
    redo_cursor: redoCursor,
    rejection: rejection === null ? null : { code: rejection },
  });
}

function authoringReceipt(
  commands: readonly TimelineCommandWire[],
  history: TimelineHistoryProjection,
): TimelineReceipt {
  const snapshot = history.snapshot;
  return decodeTimelineReceipt({
    schema: "h3.context.timeline_receipt.v1",
    request_id: "e2e-timeline-request",
    transaction_id: "e2e-timeline-transaction",
    workspace_handle: history.workspaceHandle,
    before_workspace_revision: snapshot.workspaceRevision - 1,
    after_workspace_revision: snapshot.workspaceRevision,
    before_workspace_fingerprint: fp("8"),
    after_workspace_fingerprint: snapshot.workspaceFingerprint,
    before_timeline_revision: snapshot.timelineRevision - 1,
    after_timeline_revision: snapshot.timelineRevision,
    before_timeline_fingerprint: fp("9"),
    after_timeline_fingerprint: snapshot.timelineFingerprint,
    commands,
    affected_ids: [],
    inverse: {
      kind: "restore_transaction_state",
      history_cursor: history.undoCursor ?? HISTORY_CURSOR_A,
    },
    history_cursor: history.undoCursor ?? HISTORY_CURSOR_A,
    selection: history.selection,
    snapshot: compositionFixture.snapshot,
  });
}

function authoringProjection(
  model: AuthoringHarnessModel,
  rejection: string | null,
) {
  const soundtrackState = model.soundtrackExcluded ? "excluded" : "included";
  return decodeAuthoringProjection({
    schema: "h3.context.authoring_workbench.projection.v1",
    workspace_handle: compositionFixture.snapshot.workspace_handle,
    context_source_id: "report-e2e",
    task_mode: "t2va",
    registry_fingerprint: fp("f"),
    reference: {
      revision: model.referenceRevision,
      sources: [
        {
          source_id: "img-1",
          kind: "image",
          admitted: true,
          admissible: true,
          reason: null,
          duration_milliseconds: null,
          label: "<Picture 1>",
          paired_with: null,
          derived_soundtrack: null,
        },
        {
          source_id: "vid-1",
          kind: "video",
          admitted: true,
          admissible: true,
          reason: null,
          duration_milliseconds: 5_000,
          label: "<Video 1>",
          paired_with: null,
          derived_soundtrack: soundtrackState,
          preview: {
            schema: "h3.context.authoring_source_preview.capability.v1",
            available: true,
            reason: null,
          },
        },
        {
          source_id: "aud-1",
          kind: "audio",
          admitted: true,
          admissible: true,
          reason: null,
          duration_milliseconds: 5_000,
          label: model.soundtrackExcluded ? "<Audio 1>" : null,
          paired_with: model.soundtrackExcluded ? null : "<Video 1>",
          derived_soundtrack: null,
        },
      ],
      canonical: [
        {
          source_id: "img-1",
          kind: "image",
          label: "<Picture 1>",
          paired_with: null,
        },
        {
          source_id: "vid-1",
          kind: "video",
          label: "<Video 1>",
          paired_with: null,
        },
        {
          source_id: "aud-1",
          kind: "audio",
          label: "<Audio 1>",
          paired_with: model.soundtrackExcluded ? null : "<Video 1>",
        },
      ],
      soundtracks: [
        {
          video_id: "vid-1",
          derived_state: soundtrackState,
          soundtrack_source_id: "aud-1",
        },
      ],
      queue_blockers: [],
      capacity: {
        schema: "h3-context-reference-set-authoring/1",
        aggregate_max: 12,
        aggregate_used: 3,
        aggregate_remaining: 9,
        image_used: 1,
        image_remaining: 8,
        video_used: 1,
        video_remaining: 2,
        paired_audio_used: model.soundtrackExcluded ? 0 : 1,
        paired_audio_remaining: 2,
        standalone_audio_used: model.soundtrackExcluded ? 1 : 0,
        standalone_audio_remaining: 2,
        timed: { max_duration_milliseconds: 149_687, max_frames: 3_600 },
      },
    },
    availability: {
      producer: model.availabilityProducer,
      revision: model.availabilityRevision,
    },
    timeline: {
      revision: model.timelineRevision,
      content_fingerprint: fp("0"),
      profile: {
        video_fps: 24,
        frame_grid: 51,
        audio_period_frames: 3,
        max_extent_frames: 3_600,
      },
      clips: model.clips.map((clip) => ({ ...clip, envelope: [] })),
      links: model.links,
      selection: model.selection,
      blockers: [],
    },
    rejection: rejection === null ? null : { code: rejection },
  });
}

async function syntheticAuthoringPreview(signal: AbortSignal): Promise<Blob> {
  const canvas = document.createElement("canvas");
  canvas.width = 2;
  canvas.height = 2;
  const context = canvas.getContext("2d");
  if (context === null) throw new Error("canvas unavailable");
  const stream = canvas.captureStream(0);
  const track = stream.getVideoTracks()[0] as CanvasCaptureMediaStreamTrack;
  const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp8")
    ? "video/webm;codecs=vp8"
    : "video/webm";
  const recorder = new MediaRecorder(stream, { mimeType });
  return new Promise<Blob>((resolve, reject) => {
    const chunks: Blob[] = [];
    let timer = 0;
    let frameTimer = 0;
    const dispose = () => {
      window.clearTimeout(timer);
      window.clearInterval(frameTimer);
      signal.removeEventListener("abort", abort);
      for (const track of stream.getTracks()) track.stop();
    };
    const abort = () => {
      if (recorder.state !== "inactive") recorder.stop();
    };
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size > 0) chunks.push(event.data);
    });
    recorder.addEventListener("error", () => {
      dispose();
      reject(new Error("synthetic media failed"));
    });
    recorder.addEventListener("stop", () => {
      dispose();
      if (signal.aborted) {
        reject(
          Object.assign(new Error("cancelled"), {
            disposition: "cancelled",
          }),
        );
        return;
      }
      resolve(new Blob(chunks, { type: mimeType }));
    });
    signal.addEventListener("abort", abort, { once: true });
    recorder.start(50);
    let frame = 0;
    const present = () => {
      context.fillStyle = frame % 2 === 0 ? "#000" : "#fff";
      context.fillRect(0, 0, 2, 2);
      track.requestFrame();
      frame += 1;
      if (frame < 40) return;
      window.clearInterval(frameTimer);
      if (recorder.state !== "inactive") recorder.stop();
    };
    present();
    // Forty explicitly requested frames make a bounded, non-zero-duration browser fixture.
    frameTimer = window.setInterval(present, 50);
    timer = window.setTimeout(() => {
      if (recorder.state !== "inactive") recorder.stop();
    }, 3000);
  });
}

/**
 * M22-06 provider settings, driven by a harness state machine rather than a
 * fixture. Every intent changes what the next render shows, so the browser lane
 * asserts a transition instead of the presence of an element.
 */
const PROVIDER_MODELS: Readonly<Record<string, string>> = {
  "remote.example.gpt": "gpt-4o-mini",
  "anthropic.claude_sonnet_4_6.remote": "claude-sonnet-4-6",
  "ollama.local.qwen": "qwen3:8b",
};

// A separate census exercises native metadata without changing historical journeys.
const NATIVE_PROVIDER_CENSUS =
  new URLSearchParams(window.location.search).get("native_models") === "1";
const UNKNOWN_MODEL_FACTS: ModelMetadataView = {
  model_digest: null,
  context_length: null,
  max_output_tokens: null,
  capabilities: [],
  locality: "unknown",
  display_name: null,
  created: null,
  max_input_tokens: null,
  structured_output: null,
  reasoning_mandatory: null,
  reasoning_control_supported: null,
  shutdown_date: null,
  moving_alias: null,
  family: null,
  parameter_size: null,
  quantization: null,
  license_sha256: null,
};
const NATIVE_MODEL_CENSUS: readonly DiscoveryCandidateView[] = [
  {
    identifier: "fixture/older:local",
    reason: "admitted",
    metadata: { ...UNKNOWN_MODEL_FACTS, created: 1600000000 },
  },
  {
    identifier: "fixture/unknown-a:local",
    reason: "admitted",
    metadata: UNKNOWN_MODEL_FACTS,
  },
  {
    identifier: "fixture/local:choice",
    reason: "admitted",
    metadata: {
      ...UNKNOWN_MODEL_FACTS,
      model_digest: fp("c"),
      context_length: 32768,
      max_input_tokens: 16384,
      max_output_tokens: 4096,
      capabilities: ["completion"],
      locality: "local",
      display_name: "Native choice 中文 😀 <Model>",
      created: 1800000000,
      moving_alias: true,
      shutdown_date: new Date(Date.now() + 7 * 86400000)
        .toISOString()
        .slice(0, 10),
      family: "fixture-family",
      parameter_size: "7B",
      quantization: "Q4_K_M",
      license_sha256: fp("d"),
    },
  },
  {
    identifier: "fixture/tie-a:local",
    reason: "admitted",
    metadata: { ...UNKNOWN_MODEL_FACTS, created: 1700000000 },
  },
  {
    identifier: "fixture/tie-b:local",
    reason: "admitted",
    metadata: { ...UNKNOWN_MODEL_FACTS, created: 1700000000 },
  },
  {
    identifier: "fixture/unknown-b:local",
    reason: "admitted",
    metadata: UNKNOWN_MODEL_FACTS,
  },
  ...Array.from({ length: 8 }, (_, index) => ({
    identifier: `fixture/unreported-${index}:local`,
    reason: "admitted" as const,
    metadata: UNKNOWN_MODEL_FACTS,
  })),
];

const PROVIDER_REMOTE_PROFILE = {
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
  limitations: [
    "remote_activation_pending",
    "retention_depends_on_provider_policy",
  ],
  host: "api.example.com",
  port: 443,
} as const;

const PROVIDER_ANTHROPIC_PROFILE = {
  profile_id: "anthropic.claude_sonnet_4_6.remote",
  provider_label: "Anthropic",
  family: "remote_anthropic",
  wire_dialect: "anthropic_messages",
  adapter_version: "1.0.0",
  parser_version: "h3.prompt_model.draft_json.v1",
  cost_class: "paid_remote",
  usage_receipt_required: true,
  retention_policy: "provider_policy",
  qualification_state: "qualified",
  limitations: ["retention_depends_on_provider_policy"],
  host: "api.anthropic.com",
  port: 443,
} as const;

const PROVIDER_LOCAL_PROFILE = {
  profile_id: "ollama.local.qwen",
  provider_label: "Ollama",
  family: "ollama",
  wire_dialect: "ollama_chat",
  adapter_version: "1.0.0",
  parser_version: "h3.prompt_model.draft_json.v1",
  cost_class: "local_resource",
  usage_receipt_required: false,
  retention_policy: "local_process_only",
  qualification_state: "catalog_only",
  limitations: ["runtime_identity_pending", "provider_activation_pending"],
  host: "127.0.0.1",
  port: 11434,
} as const;

const PROVIDER_EMPTY: ProviderSettingsProjection = Object.freeze({
  schema: PROVIDER_SETTINGS_SCHEMA,
  revision: 1,
  catalog_empty: true,
  profiles: [],
  selected_profile_id: "",
  selected_model_id: "",
  selected_model: null,
  readiness: "not_configured",
  disclosure: null,
  consent: null,
  consent_required: false,
  credential_required: false,
  credential_present: false,
  credential_last_four: "",
  candidates: [],
  candidates_truncated: false,
  diagnostic: null,
  reachability_observed: false,
  assisted_authoring: {
    available: false,
    selected: false,
    ready: false,
    authorized_for_this_action: false,
    defaulted: false as const,
  },
});

const PROVIDER_STOCKED: ProviderSettingsProjection = Object.freeze({
  ...PROVIDER_EMPTY,
  catalog_empty: false,
  profiles: NATIVE_PROVIDER_CENSUS
    ? [
        {
          ...PROVIDER_REMOTE_PROFILE,
          profile_id: "openai.remote",
          host: "api.openai.com",
          adapter_version: "1.2.0",
          parser_version: "h3.prompt_model.draft_json.v2",
        },
        {
          ...PROVIDER_ANTHROPIC_PROFILE,
          profile_id: "anthropic.remote",
          qualification_state: "catalog_only",
          adapter_version: "1.2.0",
          parser_version: "h3.prompt_model.draft_json.v2",
        },
        {
          ...PROVIDER_LOCAL_PROFILE,
          profile_id: "ollama.local",
          qualification_state: "qualified",
          limitations: ["text_only_drafting"],
          adapter_version: "1.2.0",
          parser_version: "h3.prompt_model.draft_json.v2",
        },
      ]
    : [
        PROVIDER_REMOTE_PROFILE,
        PROVIDER_ANTHROPIC_PROFILE,
        PROVIDER_LOCAL_PROFILE,
      ],
  candidates: [],
  assisted_authoring: {
    available: true,
    selected: false,
    ready: false,
    authorized_for_this_action: false,
    defaulted: false as const,
  },
});

function isRemoteProviderFamily(family: string): boolean {
  return family === "remote_openai_compatible" || family === "remote_anthropic";
}

function providerCandidates(profileId: string, omittedModel = "") {
  const profile = PROVIDER_STOCKED.profiles.find(
    (item) => item.profile_id === profileId,
  );
  if (profile !== undefined && NATIVE_PROVIDER_CENSUS)
    return NATIVE_MODEL_CENSUS.filter((row) => row.identifier !== omittedModel);
  return profile === undefined
    ? []
    : [
        {
          identifier: PROVIDER_MODELS[profile.profile_id]!,
          reason: "admitted" as const,
          metadata: null,
        },
        {
          identifier: "some-other-model",
          reason: "unpinned" as const,
          metadata: null,
        },
      ];
}

function providerReduce(
  current: ProviderSettingsProjection,
  intent: ProviderIntent,
  payload: ProviderIntentPayload,
  omittedModel = "",
): ProviderSettingsProjection {
  const revision = current.revision + 1;
  const inactive = {
    ...current.assisted_authoring,
    ready: false,
    authorized_for_this_action: false,
  };
  if (
    ["connect_and_refresh", "select_model", "recheck_readiness"].includes(
      intent,
    ) &&
    (payload.profile_id !== current.selected_profile_id ||
      payload.expected_revision !== current.revision)
  )
    return current;
  if (intent === "clear_selection") return { ...PROVIDER_STOCKED, revision };
  if (intent === "select_profile") {
    const profile = current.profiles.find(
      (row) => row.profile_id === payload.profile_id,
    );
    if (profile === undefined) return current;
    const remote = isRemoteProviderFamily(profile.family);
    return {
      ...PROVIDER_STOCKED,
      revision,
      selected_profile_id: profile.profile_id,
      consent_required: remote,
      credential_required: remote,
      reachability_observed: false,
      disclosure: {
        family: profile.family,
        destination: remote ? "internet" : "loopback_http",
        transfer_boundary: remote ? "remote_upload" : "ollama_process",
        preflight_required: true,
        consent_required: remote,
        local_only: !remote,
        requires_credential: remote,
        accepted_media: ["text"],
        transmits_media: false,
        consent_scope: "session_only",
        provider_id: "",
        retention_policy: profile.retention_policy,
      },
      assisted_authoring: { ...inactive, available: true, selected: true },
    };
  }
  if (intent === "connect_and_refresh") {
    const remote = current.credential_required;
    if (
      remote &&
      (!(payload.credential || current.credential_present) ||
        (payload.network_permitted !== true &&
          current.consent?.network_permitted !== true))
    )
      return current;
    const replacement = payload.credential !== undefined;
    const candidates = providerCandidates(
      current.selected_profile_id,
      omittedModel,
    );
    const missing =
      current.selected_model_id !== "" &&
      !candidates.some((row) => row.identifier === current.selected_model_id);
    return {
      ...current,
      revision,
      selected_model_id:
        replacement || missing ? "" : current.selected_model_id,
      selected_model: replacement || missing ? null : current.selected_model,
      candidates,
      diagnostic: missing
        ? {
            outcome_id: "prompt_model.model_missing",
            severity: "error",
            remediation: "select_model",
            parameters: [],
          }
        : null,
      reachability_observed: true,
      credential_present: remote,
      readiness: "not_configured",
      assisted_authoring: inactive,
      consent: remote
        ? {
            profile_id: current.selected_profile_id,
            status: "granted",
            network_permitted: true,
            media_upload_consented: false,
            revision,
            scope: "session_only",
          }
        : null,
    };
  }
  if (intent === "select_model") {
    const exact = current.candidates.filter(
      (row) => row.identifier === payload.model_id,
    );
    if (exact.length !== 1 || exact[0]!.reason !== "admitted") return current;
    return {
      ...current,
      revision,
      selected_model_id: exact[0]!.identifier,
      selected_model: {
        model_id: exact[0]!.identifier,
        metadata: exact[0]!.metadata,
      },
      readiness: "unreachable",
      reachability_observed: false,
      assisted_authoring: inactive,
    };
  }
  if (intent === "clear_model")
    return {
      ...current,
      revision,
      selected_model_id: "",
      selected_model: null,
      readiness: "not_configured",
      reachability_observed: false,
      assisted_authoring: inactive,
    };
  if (intent === "discard_credential" || intent === "revoke_consent")
    return {
      ...current,
      revision,
      credential_present:
        intent === "discard_credential" ? false : current.credential_present,
      selected_model_id: "",
      selected_model: null,
      candidates: [],
      readiness: "not_configured",
      reachability_observed: false,
      assisted_authoring: inactive,
      consent: {
        profile_id: current.selected_profile_id,
        status: "denied",
        network_permitted: false,
        media_upload_consented: false,
        revision,
        scope: "session_only",
      },
    };
  if (intent === "recheck_readiness") {
    const profile = current.profiles.find(
      (row) => row.profile_id === current.selected_profile_id,
    );
    const exact = current.candidates.filter(
      (row) => row.identifier === current.selected_model_id,
    );
    const ready =
      profile !== undefined &&
      exact.length === 1 &&
      exact[0]!.reason === "admitted" &&
      (!current.credential_required ||
        (current.credential_present &&
          current.consent?.network_permitted === true));
    return {
      ...current,
      revision,
      readiness: ready ? "ready" : "not_configured",
      reachability_observed: true,
      assisted_authoring: {
        ...inactive,
        ready,
        authorized_for_this_action:
          ready && profile?.qualification_state === "qualified",
      },
    };
  }
  return current;
}

const connectGuidanceProjection: ProductShellProjection = {
  schema: "h3.context.product.shell.v1",
  product_scope: "MANUAL_ONLY_SCOPED",
  qualification_plan_fingerprint: fp("a"),
  report_id: "connect-guidance-report",
  report_revision: 1,
  report_fingerprint: fp("b"),
  prompt_fingerprint: fp("c"),
  correlation: {
    prompt_id: "connect-guidance-prompt",
    execution_node_id: "20",
  },
  task_mode: "t2va",
  profile: "h3_base",
  host: {
    node_api: "V1_ONLY",
    core_version: "0.32.0",
    core_revision: "b323a345bbbfb2f3a95b5b73b68eb7919a26515e", // pragma: allowlist secret
    frontend_version: "1.48.7",
    frontend_revision: "6d6af63c00f132cd25dc29307fc56bd2c094fa22", // pragma: allowlist secret
  },
  native_node_id: "MiniMaxH3ImageToVideo",
  prompt_export_ready: true,
  native_queue_ready: true,
  assisted_ready: false,
  readiness_reason: "manual_only_scoped",
  field_ids: [],
  bindings: [],
  limitations: [],
  assisted_authoring: {
    available: true,
    selected: false,
    ready: false,
    authorized_for_this_action: false,
    defaulted: false,
  },
};

const newConnectDecision = (): ShellState => ({
  status: "interactive",
  reason: "incompatible_graph",
  existingGraph: true,
});

function ConnectGuidanceHarness() {
  const [locale, setLocale] = useState<Locale>("en");
  const [state, setState] = useState<ShellState>(newConnectDecision);
  const [mountKey, setMountKey] = useState(0);
  const [transactionId, setTransactionId] = useState(0);
  const [startCount, setStartCount] = useState(0);

  return (
    <main>
      <div aria-label="Connect guidance harness controls">
        <label>
          Guidance locale
          <select
            value={locale}
            onChange={(event) => setLocale(event.target.value as Locale)}
          >
            <option value="en">English</option>
            <option value="zh-TW">繁體中文</option>
            <option value="zh-CN">简体中文</option>
          </select>
        </label>
        <button
          type="button"
          disabled={state.status !== "working"}
          onClick={() => {
            if (state.status !== "working") return;
            setState({
              status: "projected",
              projection: connectGuidanceProjection,
              transactionId: state.transactionId,
            });
          }}
        >
          Complete host projection
        </button>
        <button type="button" onClick={() => setMountKey((value) => value + 1)}>
          Remount sidebar
        </button>
        <button
          type="button"
          disabled={state.status !== "projected"}
          onClick={() => {
            if (state.status !== "projected") return;
            setState({ ...state, projection: connectGuidanceProjection });
          }}
        >
          Refresh projected canvas
        </button>
        <button type="button" onClick={() => setState(newConnectDecision())}>
          New canvas decision
        </button>
        <output id="connect-guidance-state">{state.status}</output>
        <output id="connect-guidance-start-count">{startCount}</output>
      </div>
      <div id="connect-guidance-sidebar" style={{ width: "min(100%, 704px)" }}>
        <H3Sidebar
          key={mountKey}
          state={state}
          locale={locale}
          appMode={{
            capability: { status: "ready" },
            durationResolution: {
              status: "resolved",
              resolution: {
                schema: "h3.context.duration_resolution.v1",
                requested_seconds: 5,
                requested_milliseconds: 5000,
                effective_milliseconds: 5167,
                frame_count: 124,
                snapped: true,
              },
            },
            connect: {
              tier: "connect",
              anchors: [
                {
                  nodeId: 20,
                  anchorType: "MiniMaxH3ImageToVideo",
                  nested: false,
                  taskMode: "t2va",
                },
              ],
            },
            onStart: (_inputs, options) => {
              if (options?.connectExisting === undefined) return;
              const nextTransactionId = transactionId + 1;
              setTransactionId(nextTransactionId);
              setStartCount((value) => value + 1);
              setState({
                status: "working",
                phase: "materializing",
                transactionId: nextTransactionId,
              });
            },
          }}
        />
      </div>
    </main>
  );
}

const durationFixture = (
  requestedSeconds: number,
): AppModeDurationResolutionState => {
  const fixture =
    requestedSeconds === 5
      ? { effective: 5167, frames: 124 }
      : requestedSeconds === 6
        ? { effective: 6583, frames: 158 }
        : requestedSeconds === 8
          ? { effective: 8000, frames: 192 }
          : undefined;
  return fixture === undefined
    ? { status: "refused", requestedSeconds }
    : {
        status: "resolved",
        resolution: {
          schema: "h3.context.duration_resolution.v1",
          requested_seconds: requestedSeconds,
          requested_milliseconds: requestedSeconds * 1000,
          effective_milliseconds: fixture.effective,
          frame_count: fixture.frames,
          snapped: requestedSeconds * 1000 !== fixture.effective,
        },
      };
};

function DurationAuthorityHarness() {
  const [state, setState] = useState<ShellState>({
    status: "interactive",
    reason: "native_preference",
    existingGraph: true,
  });
  const [draft, setDraft] = useState<AppModeDraft>(initialAppModeDraft);
  const [durationResolution, setDurationResolution] =
    useState<AppModeDurationResolutionState>(() => durationFixture(5));
  const [submitted, setSubmitted] = useState("none");
  const [submissionCount, setSubmissionCount] = useState(0);
  const [activePrompt, setActivePrompt] = useState("none");
  const [terminalOutcome, setTerminalOutcome] = useState("none");
  const resolve = (requestedSeconds: number) => {
    setDurationResolution({ status: "resolving", requestedSeconds });
    window.setTimeout(
      () => setDurationResolution(durationFixture(requestedSeconds)),
      0,
    );
  };
  const retry = () => {
    if (durationResolution.status === "refused")
      resolve(durationResolution.requestedSeconds);
  };

  return (
    <main>
      <output id="duration-authority-submission">{submitted}</output>
      <output id="duration-authority-submission-count">
        {submissionCount}
      </output>
      <output id="duration-authority-state">{state.status}</output>
      <output id="duration-authority-terminal">{terminalOutcome}</output>
      <button
        type="button"
        onClick={() => {
          if (retainedProjectionOwnsPrompt(state, activePrompt)) {
            setTerminalOutcome("accepted");
            return;
          }
          setTerminalOutcome("projection_missing");
          setState({
            status: "error",
            code: "projection_missing",
            severity: "error",
            source: "app_mode",
            message: "projection_missing",
            recovery: "retry",
            existingGraph: true,
          });
        }}
      >
        Deliver exact success
      </button>
      <div
        id="duration-authority-sidebar"
        style={{ width: "min(100%, 320px)" }}
      >
        <H3Sidebar
          state={state}
          appMode={{
            capability: { status: "ready" },
            existingGraph: true,
            durationResolution,
            onResolveDuration: resolve,
            onRetryDuration: retry,
            onStart: (inputs, options) => {
              const nextSubmission = submissionCount + 1;
              const promptId = `prompt-${nextSubmission}`;
              setSubmitted(
                JSON.stringify({
                  user_intent: inputs.user_intent,
                  duration_milliseconds: inputs.duration_milliseconds,
                  frame_count: inputs.frame_count,
                  use_existing: options?.useExisting === true,
                }),
              );
              setSubmissionCount(nextSubmission);
              setActivePrompt(promptId);
              setTerminalOutcome("pending");
              setState({
                status: "working",
                phase: "queueing",
                transactionId: nextSubmission,
                existingGraph: true,
                graphFingerprint: "graph-existing",
              });
              window.setTimeout(
                () =>
                  setState({
                    status: "projected",
                    projection: {
                      ...validProductShell,
                      correlation: {
                        ...validProductShell.correlation,
                        prompt_id: promptId,
                      },
                    },
                    transactionId: nextSubmission,
                    graphFingerprint: "graph-existing",
                  }),
                0,
              );
            },
            onCancel: () => undefined,
          }}
          appModeDraft={draft}
          onAppModeDraftChange={setDraft}
          onEditAppModeSetup={() =>
            setState((current) =>
              reduceShellState(current, { type: "edit_setup" }),
            )
          }
          onCancelAppModeSetup={() =>
            setState((current) =>
              reduceShellState(current, {
                type: "cancel_edit",
                graphFingerprint: "graph-existing",
              }),
            )
          }
          onChooseNative={() =>
            setState({
              status: "interactive",
              reason: "native_preference",
              existingGraph: true,
            })
          }
        />
      </div>
    </main>
  );
}

/**
 * M25-41: `?mode=production&planning=1` mounts the real sidebar planning section with inert
 * actions and the storyboard review open, so its toolbar layout, hues and descriptions are measured
 * on the shipped stylesheet. Existing production journeys never pass the flag.
 */
function planningHarnessBinding(): SidebarNleBinding | undefined {
  if (new URLSearchParams(window.location.search).get("planning") !== "1")
    return undefined;
  const inert = new Proxy(
    {},
    { get: () => () => undefined },
  ) as NleWorkspaceBinding["actions"];
  return {
    surface: initialNleWorkspaceState.surface,
    supported: false,
    onOpen: () => undefined,
    onFunctionChange: () => undefined,
    requestedFunction: initialNleWorkspaceState.functionRequest,
    planning: {
      state: {
        ...initialNleWorkspaceState,
        planning: { ...initialNlePlanningState, storyboardReviewOpen: true },
      },
      contextAvailable: true,
      actions: inert,
    },
    importAction: {
      state: initialNleImportState,
      selectionState: () => ({ eligible: false, reason: null, busy: false }),
      onImport: () => undefined,
      onRetry: () => undefined,
      onOpen: () => undefined,
    },
  };
}

const PLANNING_HARNESS_NLE = planningHarnessBinding();

function ProductionHarness() {
  const [locale, setLocale] = useState<Locale>("en");
  const [selectedPage, setSelectedPage] = useState<PageId>("context");
  const [mountKey, setMountKey] = useState(0);
  const [productionState, setProductionState] = useState<ProductionViewState>({
    status: "ready",
    projection: productionProjection(
      1,
      ["segment.1", "segment.2", "segment.3"],
      ["segment.1"],
    ),
  });
  const [language, setLanguage] = useState<LocalePreference>("auto");
  const [languagePending, setLanguagePending] = useState(false);
  const [provider, setProvider] =
    useState<ProviderSettingsProjection>(PROVIDER_STOCKED);
  const [providerIntentCount, setProviderIntentCount] = useState(0);
  const providerOmittedModel = useRef("");
  const [providerRejection, setProviderRejection] = useState<
    string | undefined
  >(undefined);
  const [providerBusyIntent, setProviderBusyIntent] = useState<
    ProviderIntent | undefined
  >(undefined);
  const [lastAction, setLastAction] = useState("none");
  const [actionCount, setActionCount] = useState(0);
  const [mediaPreview, setMediaPreview] = useState<ProductionMediaPreviewState>(
    { status: "closed" },
  );
  const [authoringState, setAuthoringState] = useState<AuthoringViewState>({
    status: "absent",
  });
  const [authoringLastAction, setAuthoringLastAction] = useState("none");
  const [authoringActionCount, setAuthoringActionCount] = useState(0);
  const [authoringPreviewCount, setAuthoringPreviewCount] = useState(0);
  const [rejectNextAuthoring, setRejectNextAuthoring] = useState(false);
  const rejectNextAdjacentPreview = useRef(false);
  const authoringModel = useRef<AuthoringHarnessModel>(initialAuthoringModel());
  const acceptedAuthoringHistory =
    useRef<TimelineHistoryProjection>(authoringHistory());
  const undoSelection = useRef<readonly string[]>([]);
  const redoSelection = useRef<readonly string[]>([]);
  const openAuthoringPreview = useCallback<AuthoringPreviewOpener>(
    async (request, signal) => {
      if (rejectNextAdjacentPreview.current && request.clipId !== "clip-1") {
        rejectNextAdjacentPreview.current = false;
        throw Object.assign(new Error("synthetic adjacent preview failure"), {
          disposition: "unsupported",
        });
      }
      const blob = await syntheticAuthoringPreview(signal);
      setAuthoringPreviewCount((value) => value + 1);
      return {
        blob,
        audioDisposition: "present_bound",
      };
    },
    [],
  );
  const [proposalRows, setProposalRows] = useState<
    readonly ProductionProposalRow[]
  >([
    {
      segmentId: "segment.1",
      ordinal: 1,
      status: "not_issued",
      actionable: true,
      reviewState: { status: "closed", handle: reviewHandle },
    },
  ]);
  const current =
    "projection" in productionState ? productionState.projection : undefined;
  const dispatchProduction = async (intent: ProductionIntent) => {
    if (
      productionState.status === "pending" ||
      productionState.status === "loading"
    )
      return;
    setLastAction(intent.action);
    setActionCount((value) => value + 1);
    if (intent.action === "release_workspace") {
      setProductionState(
        current === undefined
          ? { status: "loading" }
          : { status: "pending", projection: current },
      );
      await new Promise((resolve) => setTimeout(resolve, 30));
      setProductionState({ status: "released" });
      setSelectedPage("context");
      return;
    }
    if (intent.action === "create_workspace_from_context") {
      setProductionState({ status: "loading" });
      await new Promise((resolve) => setTimeout(resolve, 30));
      setProductionState({
        status: "ready",
        projection: productionProjection(1, ["segment.1"], ["segment.1"]),
      });
      return;
    }
    if (current === undefined) return;
    setProductionState({ status: "pending", projection: current });
    await new Promise((resolve) => setTimeout(resolve, 30));
    let ids = current.segments.map((segment) => segment.segmentId);
    let selected = [...current.selectedSegmentIds];
    if (intent.action === "reorder_segments") ids = [...intent.segmentIds];
    if (intent.action === "set_selection") selected = [...intent.segmentIds];
    if (intent.action === "delete_segment") {
      ids = ids.filter((segmentId) => segmentId !== intent.segmentId);
      selected = selected.filter((segmentId) => segmentId !== intent.segmentId);
    }
    if (intent.action === "add_segment_from_context")
      ids = [...ids, `segment.${ids.length + 1}`];
    const historicalAuthorityRevision =
      intent.action === "set_selection"
        ? current.generationSequence?.workspaceRevision
        : undefined;
    const outputCount =
      current.generationSequence === null ||
      historicalAuthorityRevision !== undefined
        ? current.outputs.length
        : 0;
    const retainedPreviewSegmentIds =
      intent.action === "set_selection"
        ? current.outputs.flatMap((output) =>
            output.preview && output.segmentId !== null
              ? [output.segmentId]
              : [],
          )
        : [];
    setProductionState({
      status: "ready",
      projection: productionProjection(
        current.workspaceRevision + 1,
        ids,
        selected,
        outputCount,
        false,
        historicalAuthorityRevision,
        retainedPreviewSegmentIds,
      ),
    });
  };
  const dispatchAuthoring = async (intent: AuthoringIntent) => {
    if (
      authoringState.status === "pending" ||
      authoringState.status === "loading"
    )
      return;
    setAuthoringLastAction(intent.action);
    setAuthoringActionCount((value) => value + 1);
    if (intent.action === "create_authoring_workspace") {
      setAuthoringState({ status: "loading" });
      await new Promise((resolve) => setTimeout(resolve, 30));
      authoringModel.current = initialAuthoringModel();
      acceptedAuthoringHistory.current = authoringHistory();
      undoSelection.current = [];
      redoSelection.current = [];
      setAuthoringState({
        status: "ready",
        projection: authoringProjection(authoringModel.current, null),
      });
      return;
    }
    const accepted =
      "projection" in authoringState ? authoringState.projection : undefined;
    const history =
      "timelineHistory" in authoringState
        ? authoringState.timelineHistory
        : undefined;
    const receipt =
      "lastTimelineReceipt" in authoringState
        ? authoringState.lastTimelineReceipt
        : undefined;
    if (accepted === undefined) return;
    setAuthoringState({
      status: "pending",
      projection: accepted,
      ...(history === undefined ? {} : { timelineHistory: history }),
      ...(receipt === undefined ? {} : { lastTimelineReceipt: receipt }),
    });
    await new Promise((resolve) => setTimeout(resolve, 30));
    if (intent.action === "release_workspace") {
      setAuthoringState({ status: "released" });
      return;
    }
    const model = authoringModel.current;
    if (intent.action === "read_projection") {
      setAuthoringState({
        status: "ready",
        projection: authoringProjection(model, null),
        ...(history === undefined ? {} : { timelineHistory: history }),
        ...(receipt === undefined ? {} : { lastTimelineReceipt: receipt }),
      });
      return;
    }
    if (intent.action === "read_timeline_history") {
      setAuthoringState({
        status: "ready",
        projection: accepted,
        timelineHistory: acceptedAuthoringHistory.current,
        ...(receipt === undefined ? {} : { lastTimelineReceipt: receipt }),
      });
      return;
    }
    if (intent.action === "apply_timeline_commands") {
      if (history === undefined) {
        setAuthoringState({
          status: "error",
          projection: accepted,
          reason: "timeline_history_not_loaded",
        });
        return;
      }
      if (rejectNextAuthoring) {
        setRejectNextAuthoring(false);
        const conflict = authoringHistory(
          history.selection,
          history.undoCursor,
          history.redoCursor,
          "stale_workspace_revision",
        );
        acceptedAuthoringHistory.current = authoringHistory(
          history.selection,
          history.undoCursor,
          history.redoCursor,
        );
        setAuthoringState({
          status: "conflict",
          projection: accepted,
          timelineHistory: conflict,
        });
        return;
      }
      const command = intent.commands[0];
      let nextHistory = history;
      if (intent.commands.length === 1 && command?.kind === "select_clips") {
        const nextSelection = command.payload.clip_ids;
        if (
          Array.isArray(nextSelection) &&
          nextSelection.every((clipId) => typeof clipId === "string")
        ) {
          undoSelection.current = history.selection;
          redoSelection.current = [];
          nextHistory = authoringHistory(nextSelection, HISTORY_CURSOR_A, null);
        }
      } else if (
        intent.commands.length === 1 &&
        command?.kind === "undo" &&
        command.payload.history_cursor === history.undoCursor
      ) {
        redoSelection.current = history.selection;
        nextHistory = authoringHistory(
          undoSelection.current,
          null,
          HISTORY_CURSOR_B,
        );
      } else if (
        intent.commands.length === 1 &&
        command?.kind === "redo" &&
        command.payload.history_cursor === history.redoCursor
      ) {
        undoSelection.current = history.selection;
        nextHistory = authoringHistory(
          redoSelection.current,
          HISTORY_CURSOR_A,
          null,
        );
      }
      acceptedAuthoringHistory.current = nextHistory;
      setAuthoringState({
        status: "ready",
        projection: accepted,
        timelineHistory: nextHistory,
        lastTimelineReceipt: authoringReceipt(intent.commands, nextHistory),
      });
      return;
    }
    if (rejectNextAuthoring) {
      setRejectNextAuthoring(false);
      setAuthoringState({
        status: "conflict",
        projection: authoringProjection(model, "revision_conflict"),
        ...(history === undefined ? {} : { timelineHistory: history }),
      });
      return;
    }
    const clipById = (clipId: string) =>
      model.clips.find((clip) => clip.clip_id === clipId);
    switch (intent.action) {
      case "refresh_availability":
        model.availabilityProducer =
          "frontend.host.graphReferenceQualification";
        model.availabilityRevision += 1;
        break;
      case "exclude_soundtrack":
        model.soundtrackExcluded = true;
        model.referenceRevision += 1;
        break;
      case "include_soundtrack":
        model.soundtrackExcluded = false;
        model.referenceRevision += 1;
        break;
      case "add_source":
      case "remove_source":
      case "reorder_source":
        model.referenceRevision += 1;
        break;
      case "move_clip": {
        const clip = clipById(intent.clipId);
        if (clip !== undefined) {
          clip.start_frame = Math.max(0, clip.start_frame + intent.deltaFrames);
          clip.lane = Math.min(7, Math.max(0, clip.lane + intent.deltaLanes));
        }
        model.timelineRevision += 1;
        break;
      }
      case "move_group":
        for (const clipId of intent.clipIds) {
          const clip = clipById(clipId);
          if (clip !== undefined)
            clip.start_frame = Math.max(
              0,
              clip.start_frame + intent.deltaFrames,
            );
        }
        model.timelineRevision += 1;
        break;
      case "snap_clip": {
        const clip = clipById(intent.clipId);
        if (clip !== undefined)
          clip.start_frame = Math.round(clip.start_frame / 51) * 51;
        model.timelineRevision += 1;
        break;
      }
      case "select_clips":
        model.selection = [...intent.clipIds];
        model.timelineRevision += 1;
        break;
      case "remove_clip":
        model.clips = model.clips.filter(
          (clip) => clip.clip_id !== intent.clipId,
        );
        model.selection = model.selection.filter(
          (clipId) => clipId !== intent.clipId,
        );
        model.links = model.links.filter(
          (link) =>
            link.video_clip_id !== intent.clipId &&
            link.audio_clip_id !== intent.clipId,
        );
        model.timelineRevision += 1;
        break;
      case "add_clip": {
        model.clips.push({
          clip_id: `clip-${model.nextClipNumber}`,
          asset_id: intent.assetId,
          kind: intent.assetId.startsWith("aud") ? "audio" : "video",
          lane: intent.lane,
          start_frame: intent.startFrame,
          frames: intent.frames,
          source_start_frame: intent.sourceStartFrame,
        });
        model.nextClipNumber += 1;
        model.timelineRevision += 1;
        break;
      }
      case "trim_clip": {
        const clip = clipById(intent.clipId);
        if (clip !== undefined) {
          if (intent.edge === "end")
            clip.frames = Math.max(1, clip.frames + intent.deltaFrames);
          else {
            clip.start_frame = Math.max(
              0,
              clip.start_frame + intent.deltaFrames,
            );
            clip.source_start_frame = Math.max(
              0,
              clip.source_start_frame + intent.deltaFrames,
            );
            clip.frames = Math.max(1, clip.frames - intent.deltaFrames);
          }
        }
        model.timelineRevision += 1;
        break;
      }
      case "split_clip": {
        const clip = clipById(intent.clipId);
        if (
          clip !== undefined &&
          intent.atOffsetFrames > 0 &&
          intent.atOffsetFrames < clip.frames
        ) {
          model.clips.push({
            ...clip,
            clip_id: `clip-${model.nextClipNumber}`,
            start_frame: clip.start_frame + intent.atOffsetFrames,
            source_start_frame: clip.source_start_frame + intent.atOffsetFrames,
            frames: clip.frames - intent.atOffsetFrames,
          });
          model.nextClipNumber += 1;
          clip.frames = intent.atOffsetFrames;
        }
        model.timelineRevision += 1;
        break;
      }
      case "merge_clips": {
        const first = clipById(intent.firstClipId);
        const second = clipById(intent.secondClipId);
        if (first !== undefined && second !== undefined) {
          first.frames += second.frames;
          model.clips = model.clips.filter(
            (clip) => clip.clip_id !== intent.secondClipId,
          );
        }
        model.timelineRevision += 1;
        break;
      }
      case "link_clips":
        model.links.push({
          video_clip_id: intent.videoClipId,
          audio_clip_id: intent.audioClipId,
        });
        model.timelineRevision += 1;
        break;
      case "unlink_clips":
        model.links = model.links.filter(
          (link) => link.video_clip_id !== intent.videoClipId,
        );
        model.timelineRevision += 1;
        break;
      default:
        break;
    }
    setAuthoringState({
      status: "ready",
      projection: authoringProjection(model, null),
      ...(history === undefined ? {} : { timelineHistory: history }),
      ...(receipt === undefined ? {} : { lastTimelineReceipt: receipt }),
    });
  };
  const pageCopy = {
    en: {
      locale: "Locale",
      remount: "Remount sidebar",
      maximum: "Show maximum",
      toned: "Show fault states",
      previewable: "Show previewable",
      completed: "Show completed run",
    },
    "zh-TW": {
      locale: "語系",
      remount: "重新掛載側欄",
      maximum: "顯示上限",
      toned: "顯示故障狀態",
      previewable: "顯示可預覽輸出",
      completed: "顯示已完成執行",
    },
    "zh-CN": {
      locale: "语言",
      remount: "重新挂载侧栏",
      maximum: "显示上限",
      toned: "显示故障状态",
      previewable: "显示可预览输出",
      completed: "显示已完成运行",
    },
  }[locale];
  return (
    <main>
      <div aria-label="Production harness controls">
        <label>
          {pageCopy.locale}
          <select
            value={locale}
            onChange={(event) => setLocale(event.target.value as Locale)}
          >
            <option value="en">English</option>
            <option value="zh-TW">繁體中文</option>
            <option value="zh-CN">简体中文</option>
          </select>
        </label>
        <button type="button" onClick={() => setMountKey((value) => value + 1)}>
          {pageCopy.remount}
        </button>
        <button
          type="button"
          onClick={() => {
            const ids = Array.from(
              { length: 64 },
              (_, index) => `segment.${index + 1}`,
            );
            setProductionState({
              status: "ready",
              projection: productionProjection(8, ids, [ids[0]!], 65, false, 8),
            });
            setSelectedPage("production");
          }}
        >
          {pageCopy.maximum}
        </button>
        <button
          type="button"
          onClick={() => {
            setProductionState({
              status: "ready",
              projection: productionProjection(
                7,
                ["segment.1", "segment.2", "segment.3"],
                ["segment.1"],
                0,
                true,
              ),
            });
            setSelectedPage("production");
          }}
        >
          {pageCopy.toned}
        </button>
        <button
          type="button"
          onClick={() => {
            setMediaPreview({ status: "closed" });
            setProductionState({
              status: "ready",
              projection: productionProjection(
                2,
                ["segment.1", "segment.2", "segment.3"],
                ["segment.1"],
                1,
                false,
                2,
              ),
            });
            setSelectedPage("production");
          }}
        >
          {pageCopy.previewable}
        </button>
        <button
          type="button"
          onClick={() => {
            setMediaPreview({ status: "closed" });
            setProductionState({
              status: "ready",
              projection: productionProjection(
                4,
                ["segment.1", "segment.2", "segment.3"],
                ["segment.1"],
                4,
                false,
                4,
                ["segment.1", "segment.2"],
              ),
            });
            setSelectedPage("production");
          }}
        >
          {pageCopy.completed}
        </button>
        <button
          type="button"
          aria-pressed={rejectNextAuthoring}
          onClick={() => setRejectNextAuthoring(true)}
        >
          Reject next authoring command
        </button>
        <button
          type="button"
          onClick={() => {
            rejectNextAdjacentPreview.current = true;
          }}
        >
          Reject next adjacent preview
        </button>
        <output id="production-last-action">{lastAction}</output>
        <output id="production-action-count">{actionCount}</output>
        <output id="authoring-last-action">{authoringLastAction}</output>
        <output id="authoring-action-count">{authoringActionCount}</output>
        <output id="authoring-preview-count">{authoringPreviewCount}</output>
        <button
          type="button"
          onClick={() => {
            setProvider(PROVIDER_EMPTY);
            setProviderRejection(undefined);
          }}
        >
          Show shipped provider default
        </button>
        <button
          type="button"
          onClick={() =>
            setProvider((current) => {
              const profile = current.profiles.find(
                (item) => item.profile_id === current.selected_profile_id,
              );
              if (profile === undefined) return current;
              const duplicate = {
                identifier: PROVIDER_MODELS[profile.profile_id]!,
                metadata: null,
                reason: "admitted" as const,
              };
              return {
                ...current,
                revision: current.revision + 1,
                selected_model_id: "",
                selected_model: null,
                readiness: "not_configured",
                candidates: [duplicate, duplicate],
                reachability_observed: true,
                assisted_authoring: {
                  ...current.assisted_authoring,
                  ready: false,
                  authorized_for_this_action: false,
                },
              };
            })
          }
        >
          Show duplicate provider census
        </button>
        <button
          type="button"
          onClick={() => setProviderRejection("credential_rejected")}
        >
          Refuse next provider credential
        </button>
        <output id="provider-revision">{provider.revision}</output>
        <output id="provider-intent-count">{providerIntentCount}</output>
        {NATIVE_PROVIDER_CENSUS ? (
          <button
            type="button"
            onClick={() => {
              providerOmittedModel.current = provider.selected_model_id;
            }}
          >
            Remove selected native model from census
          </button>
        ) : null}
      </div>
      <div
        id="production-sidebar-container"
        style={{ width: "min(100%, 960px)" }}
      >
        <H3Sidebar
          key={mountKey}
          nle={PLANNING_HARNESS_NLE}
          state={{ status: "interactive", reason: "native_preference" }}
          locale={locale}
          pageRegistry={{
            selected: selectedPage,
            pages: [
              { id: "context" },
              { id: "production" },
              { id: "settings" },
            ],
          }}
          onSelectPage={setSelectedPage}
          productionState={productionState}
          contextWorkspaceHandle={`ws_${"c".repeat(43)}`}
          onProductionIntent={dispatchProduction}
          authoringState={authoringState}
          onAuthoringIntent={dispatchAuthoring}
          onAuthoringMediaPreview={openAuthoringPreview}
          productionProposalRows={proposalRows}
          productionMediaPreview={mediaPreview}
          onProductionMediaPreview={(outputHandle) => {
            if (current === undefined) return;
            // IMPORTANT (B-M1605-PREVIEW-01): the real client shows loading while the bounded
            // media route answers. Jumping straight to ready hid a player that grew below the
            // loading-time reveal.
            const workspaceRevision = current.workspaceRevision;
            setMediaPreview({
              status: "loading",
              workspaceRevision,
              outputHandle,
            });
            setTimeout(
              () =>
                setMediaPreview((preview) =>
                  preview.status === "loading" &&
                  preview.outputHandle === outputHandle
                    ? {
                        status: "ready",
                        workspaceRevision,
                        outputHandle,
                        url: "blob:synthetic-e2e-preview",
                      }
                    : preview,
                ),
              120,
            );
          }}
          onProductionMediaPreviewClose={() =>
            setMediaPreview({ status: "closed" })
          }
          onProductionProposalRead={async (segmentIds) => {
            const selected = new Set(segmentIds);
            setProposalRows((rows) =>
              rows.map((row) =>
                selected.has(row.segmentId)
                  ? {
                      ...row,
                      status: "loading",
                      reviewState: { status: "loading", handle: reviewHandle },
                    }
                  : row,
              ),
            );
            await new Promise((resolve) => setTimeout(resolve, 30));
            setProposalRows((rows) =>
              rows.map((row) =>
                selected.has(row.segmentId)
                  ? {
                      ...row,
                      status: "ready",
                      reviewState: {
                        status: "ready",
                        handle: reviewHandle,
                        projection: initialReview,
                      },
                    }
                  : row,
              ),
            );
          }}
          onProductionProposalClose={(segmentId) =>
            setProposalRows((rows) =>
              rows.map((row) =>
                row.segmentId === segmentId
                  ? {
                      ...row,
                      status: "not_issued",
                      reviewState: { status: "closed", handle: reviewHandle },
                    }
                  : row,
              ),
            )
          }
          onProductionProposalAction={async (segmentId, request) => {
            if (request.action !== "proposal_accept") return;
            setProposalRows((rows) =>
              rows.map((row) =>
                row.segmentId === segmentId
                  ? {
                      ...row,
                      status: "terminal",
                      reviewState: {
                        status: "ready",
                        handle: reviewHandle,
                        projection: acceptedReview,
                      },
                    }
                  : row,
              ),
            );
          }}
          providerSettings={provider}
          providerRejection={providerRejection}
          providerBusy={providerBusyIntent !== undefined}
          providerBusyIntent={providerBusyIntent}
          onProviderIntent={async (intent, payload = {}) => {
            setProviderIntentCount((count) => count + 1);
            setProviderRejection(undefined);
            if (
              [
                "recheck_readiness",
                "connect_and_refresh",
                "select_model",
              ].includes(intent)
            ) {
              setProviderBusyIntent(
                intent === "select_model" ? "recheck_readiness" : intent,
              );
              await new Promise((resolve) => setTimeout(resolve, 80));
            }
            setProvider((current) => {
              const selected = providerReduce(
                current,
                intent,
                payload,
                providerOmittedModel.current,
              );
              return intent === "select_model" && selected !== current
                ? providerReduce(selected, "recheck_readiness", {
                    profile_id: selected.selected_profile_id,
                    expected_revision: selected.revision,
                  })
                : selected;
            });
            setProviderBusyIntent(undefined);
          }}
          languageSettings={{
            status: "ready",
            value: language,
            pending: languagePending,
          }}
          onLanguageWrite={async (value) => {
            setLanguagePending(true);
            await new Promise((resolve) => setTimeout(resolve, 30));
            setLanguage(value);
            setLocale(value === "auto" ? "en" : value);
            setLanguagePending(false);
            return true;
          }}
        />
      </div>
    </main>
  );
}

const assistedReferenceCandidates = [
  ...Array.from({ length: 9 }, (_, index) => ({
    asset_id: `image_${index + 1}`,
    kind: "image" as const,
    label: `<Picture ${index + 1}>`,
    ordinal: index + 1,
    paired_with: null,
  })),
  ...Array.from({ length: 3 }, (_, index) => ({
    asset_id: `video_${index + 1}`,
    kind: "video" as const,
    label: `<Video ${index + 1}>`,
    ordinal: index + 1,
    paired_with: null,
  })),
  ...Array.from({ length: 6 }, (_, index) => ({
    asset_id: `audio_${index + 1}`,
    kind: "audio" as const,
    label: `<Audio ${index + 1}>`,
    ordinal: index + 1,
    paired_with: index < 3 ? `<Video ${index + 1}>` : null,
  })),
];
const assistedSubjectCandidates = Array.from({ length: 46 }, (_, index) => ({
  subject_id: `subject_${index + 1}`,
  ordinal: index + 1,
  label: `<Subject ${index + 1}>`,
  display: `declared subject ${index + 1}`,
}));
const assistedTokenWorkspace = {
  ...validSidebarWorkspace,
  reference_candidates: assistedReferenceCandidates,
  subject_candidates: assistedSubjectCandidates,
} satisfies SidebarWorkspaceProjection;

function AssistedDraftHarness() {
  const deferredRequests =
    new URLSearchParams(window.location.search).get("defer_assisted") === "1";
  const requestGeneration = useRef(0);
  const deferredCompletion = useRef<(() => void) | undefined>(undefined);
  const requestedLocale = new URLSearchParams(window.location.search).get(
    "locale",
  );
  const locale =
    requestedLocale === "zh-TW" || requestedLocale === "zh-CN"
      ? requestedLocale
      : "en";
  const [actionCount, setActionCount] = useState(0);
  const [lastAction, setLastAction] = useState("");
  const [workspace, setWorkspace] = useState<SidebarWorkspaceProjection>(
    assistedTokenWorkspace,
  );
  const [proposal, setProposal] = useState<
    AssistedPromptProposalProjection | undefined
  >(undefined);
  const [busy, setBusy] = useState(false);
  const workspaceState: SidebarWorkspaceState = {
    status: "ready",
    projection: workspace,
  };
  const provider = {
    ...PROVIDER_STOCKED,
    assisted_authoring: {
      available: true,
      selected: true,
      ready: true,
      authorized_for_this_action: true,
      defaulted: false as const,
    },
  };
  const handle = async (request: WorkspaceActionRequest): Promise<void> => {
    setActionCount((value) => value + 1);
    setLastAction(request.action);
    if (request.action === "stage_prompt") {
      const promptText = request.payload.prompt_text;
      if (typeof promptText !== "string") return;
      setWorkspace((current) => ({
        ...current,
        report_revision: current.report_revision + 1,
        report_fingerprint: fp("5"),
        prompt_fingerprint: fp("4"),
        prompt_text: promptText,
        lifecycle: "stale",
        validation_status: "not_run",
        bindings: [],
        media_receipt: {
          ...current.media_receipt,
          queue_ready: false,
          binding_count: 0,
        },
        proposal: {
          ...current.proposal,
          changed: true,
          reason:
            typeof request.payload.reason === "string"
              ? request.payload.reason
              : null,
          current_prompt_fingerprint: fp("4"),
          diff: { status: "changed", lines: [] },
        },
        stages: current.stages.map((stage, index) => ({
          ...stage,
          status:
            index === 3
              ? ("active" as const)
              : index === 4
                ? ("blocked" as const)
                : ("complete" as const),
        })),
        actions: {
          ...current.actions,
          validate: true,
          export: false,
          copy_prompt: false,
        },
      }));
      return;
    }
    if (request.action === "validate") {
      setWorkspace((current) => ({
        ...current,
        report_fingerprint: fp("6"),
        lifecycle: "ready",
        validation_status: "passed",
        bindings: validSidebarWorkspace.bindings,
        diagnostics: [],
        media_receipt: {
          ...current.media_receipt,
          queue_ready: true,
          binding_count: 1,
        },
        stages: validSidebarWorkspace.stages,
        actions: {
          ...current.actions,
          validate: false,
          export: true,
          copy_prompt: true,
        },
      }));
      return;
    }
    if (
      request.action === "optimize_prompt" ||
      request.action === "refine_prompt"
    ) {
      const generation = ++requestGeneration.current;
      setBusy(true);
      await new Promise<void>((resolve) => {
        if (deferredRequests) deferredCompletion.current = resolve;
        else setTimeout(resolve, 40);
      });
      if (generation !== requestGeneration.current) return;
      deferredCompletion.current = undefined;
      setProposal({
        schema: "h3.context.assisted_prompt_proposal.v1",
        proposal_id: `assist_${"a".repeat(32)}`,
        proposal_revision: 1,
        state: "active",
        workspace_id: workspace.workspace_id,
        report_revision: workspace.report_revision,
        report_fingerprint: workspace.report_fingerprint,
        prompt_fingerprint: fp("8"),
        candidate_text: `${workspace.prompt_text}\nCamera: A deliberate slow push.`,
        audit: {
          schema: "h3.context.prompt_fidelity.v2",
          length_band: "within_target",
          description_characters: 512,
          diagnostics: [],
        },
        receipt: {
          schema: "h3.context.assisted_draft.receipt.v2",
          downgraded: false,
          observed_model_id: "fixture-model",
          action_id: `action_${"b".repeat(32)}`,
          profile_id: "local.test.profile",
          provider_family: "local_ollama",
          model_id: "test-model",
          attempts: 1,
          outcome_id: "prompt_model.ok",
          requests: 1,
          request_bytes: 100,
          response_bytes: 80,
          prompt_tokens: 20,
          completion_tokens: 10,
          duration_ms: 30,
          evidence_fingerprint: fp("9"),
          provider_revision: 4,
        },
      });
      setBusy(false);
      return;
    }
    if (request.action === "edit_assisted_proposal" && proposal !== undefined) {
      setProposal({
        ...proposal,
        proposal_revision: proposal.proposal_revision + 1,
        candidate_text: request.payload.prompt_text,
        prompt_fingerprint: fp("7"),
      });
      return;
    }
    if (
      request.action === "accept_assisted_proposal" &&
      proposal !== undefined
    ) {
      setWorkspace({
        ...workspace,
        report_revision: workspace.report_revision + 1,
        report_fingerprint: fp("6"),
        prompt_fingerprint: proposal.prompt_fingerprint,
        prompt_text: proposal.candidate_text,
      });
      setProposal(undefined);
      return;
    }
    if (
      request.action === "reject_assisted_proposal" ||
      request.action === "cancel_assisted_execution"
    ) {
      ++requestGeneration.current;
      deferredCompletion.current?.();
      deferredCompletion.current = undefined;
      setProposal(undefined);
      setBusy(false);
    }
  };
  return (
    <main>
      <div style={{ overflowWrap: "anywhere" }}>
        <output id="canonical-prompt">{workspace.prompt_text}</output>
        <output id="workspace-fingerprint">
          {workspace.report_fingerprint}
        </output>
        <output id="validation-status">{workspace.validation_status}</output>
        <output id="assisted-action-count">{actionCount}</output>
        <output id="assisted-last-action">{lastAction}</output>
      </div>
      {deferredRequests && (
        <button
          type="button"
          disabled={!busy}
          onClick={() => deferredCompletion.current?.()}
        >
          Finish deferred assisted request
        </button>
      )}
      <div style={{ width: "100%" }}>
        <H3Sidebar
          state={{ status: "projected", projection: validProductShell }}
          workspaceState={workspaceState}
          providerSettings={provider}
          assistedProposal={proposal}
          assistedBusy={busy}
          locale={locale}
          onWorkspaceAction={handle}
        />
      </div>
    </main>
  );
}

function Harness() {
  const [locale, setLocale] = useState<Locale>("en");
  const [variant, setVariant] = useState<"prepared" | "unknown">("prepared");
  const [mountKey, setMountKey] = useState(0);
  const [lastAction, setLastAction] = useState<TransactionIntent | "none">(
    "none",
  );
  const [reviewState, dispatchReview] = useReducer(
    reduceSemanticProposalReviewState,
    { status: "closed", handle: reviewHandle },
  );
  const [serverReview, setServerReview] =
    useState<SemanticProposalReviewProjection>(initialReview);
  const [readCount, setReadCount] = useState(0);
  const [actionCount, setActionCount] = useState(0);
  const [delayResponses, setDelayResponses] = useState(false);
  const [failNextRead, setFailNextRead] = useState(false);
  const deliver = (action: () => void) => {
    if (delayResponses) setTimeout(action, 300);
    else queueMicrotask(action);
  };
  const projection = decodeTransactionTransparencyProjection(
    variant === "prepared" ? prepared : unknown,
  );
  return (
    <main>
      <div aria-label="Harness controls">
        <label>
          Locale
          <select
            value={locale}
            onChange={(event) => setLocale(event.target.value as Locale)}
          >
            <option value="en">English</option>
            <option value="zh-TW">繁體中文</option>
            <option value="zh-CN">简体中文</option>
          </select>
        </label>
        <button type="button" onClick={() => setVariant("unknown")}>
          Show unknown ownership
        </button>
        <button type="button" onClick={() => setMountKey((value) => value + 1)}>
          Remount sidebar
        </button>
        <button
          type="button"
          onClick={() => {
            setServerReview(clarificationReview);
            dispatchReview({ type: "host", handle: reviewHandle });
          }}
        >
          Prepare clarification
        </button>
        <button type="button" onClick={() => setFailNextRead(true)}>
          Fail next proposal read
        </button>
        <button
          type="button"
          aria-pressed={delayResponses}
          onClick={() => setDelayResponses((value) => !value)}
        >
          Delay proposal responses
        </button>
        <button
          type="button"
          onClick={() =>
            dispatchReview({ type: "host", handle: successorReviewHandle })
          }
        >
          Switch proposal handle
        </button>
        <output id="last-action">{lastAction}</output>
        <output id="proposal-read-count">{readCount}</output>
        <output id="proposal-action-count">{actionCount}</output>
      </div>
      <div style={{ width: "min(100%, 520px)" }}>
        <H3Sidebar
          key={mountKey}
          state={{ status: "interactive", reason: "native_preference" }}
          locale={locale}
          transactionTransparency={projection}
          onTransactionIntent={setLastAction}
          semanticProposalReview={reviewState}
          onSemanticProposalOpen={() => {
            dispatchReview({ type: "open" });
            setReadCount((value) => value + 1);
            if (failNextRead) {
              setFailNextRead(false);
              deliver(() => dispatchReview({ type: "failed" }));
            } else {
              deliver(() =>
                dispatchReview({
                  type: "received",
                  result: decodeSemanticProposalActionResult({
                    schema: "h3.context.semantic_proposal.action_result.v1",
                    outcome: "read",
                    reason: "review_current",
                    review: serverReview,
                  }),
                }),
              );
            }
          }}
          onSemanticProposalClose={() => dispatchReview({ type: "close" })}
          onSemanticProposalAction={({ action }) => {
            setActionCount((value) => value + 1);
            dispatchReview({ type: "request", action });
            const outcome = {
              proposal_resolve: "resolved",
              proposal_accept: "accepted",
              proposal_reject: "rejected",
              proposal_cancel: "cancelled",
            }[action as SemanticProposalMutation] as
              "resolved" | "accepted" | "rejected" | "cancelled";
            const terminal = outcome === "resolved" ? null : outcome;
            const next = decodeSemanticProposalReviewProjection({
              ...serverReview,
              transaction_fingerprint: fp("3"),
              workspace_revision:
                outcome === "accepted"
                  ? serverReview.workspace_revision + 1
                  : serverReview.workspace_revision,
              workspace_fingerprint:
                outcome === "accepted"
                  ? fp("4")
                  : serverReview.workspace_fingerprint,
              state: outcome === "resolved" ? "ready_for_review" : outcome,
              terminal,
              clarifications:
                outcome === "resolved" ? [] : serverReview.clarifications,
              uncertainty_codes:
                outcome === "resolved" ? [] : serverReview.uncertainty_codes,
              reason_code:
                outcome === "resolved"
                  ? "proposal_ready"
                  : `proposal_${outcome}`,
              actions: {
                proposal_read: true,
                proposal_resolve: false,
                proposal_accept: outcome === "resolved",
                proposal_reject: outcome === "resolved",
                proposal_cancel: outcome === "resolved",
                edit: false,
                regenerate: false,
              },
            });
            setServerReview(next);
            deliver(() =>
              dispatchReview({
                type: "received",
                result: decodeSemanticProposalActionResult({
                  schema: "h3.context.semantic_proposal.action_result.v1",
                  outcome,
                  reason: `proposal_${outcome}`,
                  review: next,
                }),
              }),
            );
          }}
        />
      </div>
    </main>
  );
}

function RefusalReasonHarness() {
  const [state, setState] = useState<ShellState>({
    status: "interactive",
    reason: "incompatible_graph",
    existingGraph: true,
    refusalReason: {
      kind: "anchor_missing",
      requiredNode: "MiniMaxH3ImageToVideo",
    },
  });

  return (
    <main>
      <div aria-label="Typed refusal harness controls">
        <button
          type="button"
          onClick={() =>
            setState({
              status: "interactive",
              reason: "incompatible_graph",
              existingGraph: true,
              refusalReason: {
                kind: "anchor_missing",
                requiredNode: "MiniMaxH3ImageToVideo",
              },
            })
          }
        >
          Show anchor refusal
        </button>
        <button
          type="button"
          onClick={() =>
            setState({
              status: "error",
              code: "incompatible_seam",
              severity: "error",
              source: "seam",
              message: "incompatible_seam",
              recovery: "use_native",
              existingGraph: true,
              refusalReason: {
                kind: "generation_admission_refused",
                admissionReason: "missing_asset",
                unsatisfiedSlots: ["video_unet", "audio_vae"],
              },
            })
          }
        >
          Show generation refusal
        </button>
      </div>
      <div id="refusal-reason-sidebar" style={{ width: "min(100%, 704px)" }}>
        <H3Sidebar
          state={state}
          appMode={{
            capability: { status: "ready" },
            durationResolution: {
              status: "resolved",
              resolution: {
                schema: "h3.context.duration_resolution.v1",
                requested_seconds: 5,
                requested_milliseconds: 5000,
                effective_milliseconds: 5167,
                frame_count: 124,
                snapped: true,
              },
            },
            onStart: () => undefined,
          }}
        />
      </div>
    </main>
  );
}

const root = document.getElementById("root");
if (root === null) throw new Error("harness root is missing");
const harnessSearch = new URLSearchParams(window.location.search);
const mode = harnessSearch.get("mode");
if (mode === "managed-sequence") {
  void import("./managedSequence")
    .then(({ mountManagedSequenceHarness }) =>
      mountManagedSequenceHarness(root),
    )
    .catch((error: unknown) => {
      root.dataset.managedSequenceError = "true";
      root.textContent =
        error instanceof Error
          ? `Managed sequence harness failed: ${error.message}`
          : "Managed sequence harness failed.";
    });
} else if (mode === "managed-entry" || mode === "managed-diagnostics") {
  const requestedQueueWrapper = harnessSearch.get("queueWrapper");
  const queuePromptWrapperMode =
    requestedQueueWrapper === "mutate_in_place" ||
    requestedQueueWrapper === "forward_copy"
      ? requestedQueueWrapper
      : "none";
  void import("./managedEntry")
    .then(({ mountManagedEntryHarness }) =>
      mountManagedEntryHarness(root, {
        artifactFailure: mode === "managed-diagnostics",
        assetRelocated:
          harnessSearch.get("generationProfile") === "asset_relocated",
        missingAsset:
          harnessSearch.get("generationProfile") === "missing_asset",
        queuePromptWrapperMode,
        durationSeconds:
          harnessSearch.get("durationSeconds") === null
            ? undefined
            : Number(harnessSearch.get("durationSeconds")),
        initialGraph:
          harnessSearch.get("initialGraph") === "empty"
            ? "empty"
            : harnessSearch.get("initialGraph") === "dirty"
              ? "dirty"
              : "existing",
        queueRejection: harnessSearch.get("queueReject") === "true",
      }),
    )
    .catch((error: unknown) => {
      root.dataset.managedEntryError = "true";
      root.textContent =
        error instanceof Error
          ? `Managed entry harness failed: ${error.message}`
          : "Managed entry harness failed.";
    });
} else {
  createRoot(root).render(
    mode === "production" ? (
      <ProductionHarness />
    ) : mode === "refusal-reasons" ? (
      <RefusalReasonHarness />
    ) : mode === "assisted" ? (
      <AssistedDraftHarness />
    ) : mode === "connect-guidance" ? (
      <ConnectGuidanceHarness />
    ) : mode === "duration-authority" ? (
      <DurationAuthorityHarness />
    ) : (
      <Harness />
    ),
  );
}
