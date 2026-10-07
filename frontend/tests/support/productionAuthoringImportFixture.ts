// Shared M25-29 import request/response builders (codec, client and NLE workspace tests).
import { projectionWire, sourceWire } from "./authoringFixture";
import compositionFixture from "../../../tests/fixtures/m25_10_composition_contract_v1.json";
import { compositionContractFingerprint } from "../../src/contracts/compositionCodec";

export const importFingerprint = `sha256:${"a".repeat(64)}`;
const fp = importFingerprint;

export function importRequest() {
  return {
    requestId: "import.1",
    productionWorkspaceHandle: `pw_${"p".repeat(32)}`,
    productionWorkspaceId: "workspace.1",
    expectedProductionWorkspaceRevision: 7,
    expectedProductionWorkspaceFingerprint: fp,
    authoringWorkspaceHandle: `authoring-${"a".repeat(32)}`,
    expectedAuthoringRegistryFingerprint: fp,
    expectedAuthoringReferenceRevision: 3,
    expectedAuthoringTimelineRevision: 4,
    expectedAuthoringTimelineContentFingerprint: fp,
    expectedNleWorkspaceRevision: 3,
    expectedNleTimelineRevision: 4,
    expectedNleTimelineFingerprint: fp,
    expectedNlePublicFingerprint: fp,
    entries: [
      { segmentId: "segment.1", outputHandle: `out_${"1".repeat(40)}` },
    ],
  } as const;
}

export function importResponseWire() {
  const authoringProjection = projectionWire({
    workspace_handle: `authoring-${"a".repeat(32)}`,
    registry_fingerprint: fp,
  });
  const reference = authoringProjection.reference as Record<string, unknown>;
  const sources = reference.sources as Record<string, unknown>[];
  sources.push(
    sourceWire({
      source_id: "generated.asset.1",
      derived_soundtrack: null,
      label: "<Video 2>",
    }),
  );
  const canonical = reference.canonical as Record<string, unknown>[];
  canonical.push({
    source_id: "generated.asset.1",
    kind: "video",
    label: "<Video 2>",
    paired_with: null,
  });
  const capacity = reference.capacity as Record<string, unknown>;
  capacity.aggregate_used = 4;
  capacity.aggregate_remaining = 8;
  capacity.video_used = 2;
  capacity.video_remaining = 1;
  return {
    schema: "h3.context.production_authoring_import.response.v1",
    receipt: {
      schema: "h3.context.production_authoring_import.receipt.v1",
      request_id: "import.1",
      disposition: "created",
      production_workspace_id: "workspace.1",
      production_workspace_revision: 7,
      production_workspace_fingerprint: fp,
      authoring_workspace_handle: `authoring-${"a".repeat(32)}`,
      authoring_registry_fingerprint: fp,
      reference: {
        prior_revision: 3,
        next_revision: 4,
        prior_fingerprint: fp,
        next_fingerprint: `sha256:${"b".repeat(64)}`,
      },
      legacy_timeline: {
        prior_revision: 2,
        next_revision: 2,
        prior_content_fingerprint: `sha256:${"0".repeat(64)}`,
        next_content_fingerprint: `sha256:${"0".repeat(64)}`,
      },
      nle: {
        prior_workspace_revision: 3,
        next_workspace_revision: 4,
        prior_workspace_fingerprint: fp,
        next_workspace_fingerprint: `sha256:${"c".repeat(64)}`,
        prior_timeline_revision: 2,
        next_timeline_revision: 2,
        prior_timeline_fingerprint: fp,
        next_timeline_fingerprint: fp,
        prior_public_fingerprint: fp,
        next_public_fingerprint: `sha256:${"d".repeat(64)}`,
      },
      rows: [
        {
          segment_id: "segment.1",
          output_handle: `out_${"1".repeat(40)}`,
          asset_id: "generated.asset.1",
          source_kind: "video",
          disposition: "created",
        },
      ],
      authority_versions: [
        "h3.context.production_authoring_import.request.v1",
        "h3.context.production_authoring_import.receipt.v1",
        "h3.context.segment_artifact_receipt.v1",
        "h3.context.authoring_source.generated.v1",
      ],
    },
    authoring_projection: authoringProjection,
  };
}

export function importV2Request() {
  const projection = projectionWire({
    workspace_handle: `authoring-${"a".repeat(32)}`,
    registry_fingerprint: fp,
  });
  const timelineFingerprint = compositionContractFingerprint({
    operation_profile_id: "h3.authoring.nle_operation.v2",
    edit_capacity_frames: 3_600,
    content_end_exclusive: 0,
    tracks: [
      {
        track_id: "track.primary",
        kind: "primary_video",
        order: 0,
        enabled: true,
        locked: false,
      },
    ],
    clips: [],
    audio_extension: {
      schema: "h3.authoring.independent_audio_extension.v1",
      track_profile: "none_v1",
      command_namespace: "h3.authoring.audio.command.v1",
      command_members: [],
      preview_edit_capability: "unsupported",
      final_render_edit_capability: "unsupported",
      embedded_renderer_variant: "EmbeddedAudioSpanV1",
      independent_audio_renderer_variant: "none_v1",
      reason: "audio_editing_deferred",
    },
  });
  return {
    requestId: "import.1",
    productionWorkspaceHandle: `pw_${"p".repeat(32)}`,
    productionWorkspaceId: "workspace.1",
    expectedProductionWorkspaceRevision: 7,
    expectedProductionWorkspaceFingerprint: fp,
    authoringWorkspaceHandle: `authoring-${"a".repeat(32)}`,
    expectedAuthoringRegistryFingerprint: fp,
    expectedAuthoringReferenceRevision: 3,
    expectedAuthoringTimelineRevision: 2,
    expectedAuthoringTimelineContentFingerprint: (
      projection.timeline as Record<string, unknown>
    ).content_fingerprint as string,
    expectedNleWorkspaceRevision: 3,
    expectedNleTimelineRevision: 2,
    expectedNleTimelineFingerprint: timelineFingerprint,
    expectedNleAuthoringFingerprint: fp,
    authoringSchema: "h3.context.nle_authoring_state.v1" as const,
    profileId: "h3.authoring.nle_content_extent.v1" as const,
    entries: [
      { segmentId: "segment.1", outputHandle: `out_${"1".repeat(40)}` },
    ],
  } as const;
}

export function importV2ResponseWire() {
  const legacy = importResponseWire();
  const authoringProjection = legacy.authoring_projection;
  const projectionTimeline = authoringProjection.timeline as Record<
    string,
    unknown
  >;
  const assets = JSON.parse(
    JSON.stringify(compositionFixture.snapshot.assets),
  ) as Record<string, unknown>[];
  const video = assets.find((asset) => asset.kind === "video");
  if (video === undefined)
    throw new Error("composition fixture has no video asset");
  const importedAsset = { ...video, asset_id: "generated.asset.1" };
  const authoring: Record<string, unknown> = {
    schema: "h3.context.nle_authoring_state.v1",
    profile_id: "h3.authoring.nle_content_extent.v1",
    operation_profile_id: "h3.authoring.nle_operation.v2",
    project_id: "project.1",
    workspace_handle: `authoring-${"a".repeat(32)}`,
    workspace_revision: 4,
    workspace_fingerprint: "",
    timeline_revision: 2,
    timeline_fingerprint: "",
    authoring_fingerprint: "",
    edit_capacity_frames: 3_600,
    content_end_exclusive: 0,
    assets: [importedAsset],
    tracks: [
      {
        track_id: "track.primary",
        kind: "primary_video",
        order: 0,
        enabled: true,
        locked: false,
      },
    ],
    clips: [],
    audio_extension: {
      schema: "h3.authoring.independent_audio_extension.v1",
      track_profile: "none_v1",
      command_namespace: "h3.authoring.audio.command.v1",
      command_members: [],
      preview_edit_capability: "unsupported",
      final_render_edit_capability: "unsupported",
      embedded_renderer_variant: "EmbeddedAudioSpanV1",
      independent_audio_renderer_variant: "none_v1",
      reason: "audio_editing_deferred",
    },
    blockers: [],
  };
  authoring.timeline_fingerprint = compositionContractFingerprint({
    operation_profile_id: authoring.operation_profile_id,
    edit_capacity_frames: authoring.edit_capacity_frames,
    content_end_exclusive: authoring.content_end_exclusive,
    tracks: authoring.tracks,
    clips: authoring.clips,
    audio_extension: authoring.audio_extension,
  });
  authoring.workspace_fingerprint = compositionContractFingerprint({
    project_id: authoring.project_id,
    workspace_handle: authoring.workspace_handle,
    workspace_revision: authoring.workspace_revision,
    timeline_revision: authoring.timeline_revision,
    timeline_fingerprint: authoring.timeline_fingerprint,
  });
  const authoringMaterial = { ...authoring };
  delete authoringMaterial.authoring_fingerprint;
  authoring.authoring_fingerprint =
    compositionContractFingerprint(authoringMaterial);
  const receipt = legacy.receipt as Record<string, unknown>;
  const v1Reference = receipt.reference as Record<string, unknown>;
  const v1Legacy = receipt.legacy_timeline as Record<string, unknown>;
  const v1Row = (receipt.rows as Record<string, unknown>[])[0]!;
  const nextReferenceFingerprint = v1Reference.next_fingerprint;
  const nextWorkspaceFingerprint = authoring.workspace_fingerprint;
  const nextAuthoringFingerprint = authoring.authoring_fingerprint;
  return {
    schema: "h3.context.production_authoring_import.response.v2",
    receipt: {
      schema: "h3.context.production_authoring_import.receipt.v2",
      request_id: "import.1",
      disposition: "created",
      production_workspace_id: "workspace.1",
      production_workspace_revision: 7,
      production_workspace_fingerprint: fp,
      authoring_workspace_handle: `authoring-${"a".repeat(32)}`,
      authoring_registry_fingerprint: fp,
      reference: {
        prior_revision: 3,
        next_revision: 4,
        prior_fingerprint: fp,
        next_fingerprint: nextReferenceFingerprint,
      },
      legacy_timeline: {
        prior_revision: projectionTimeline.revision,
        next_revision: projectionTimeline.revision,
        prior_content_fingerprint: projectionTimeline.content_fingerprint,
        next_content_fingerprint: projectionTimeline.content_fingerprint,
      },
      nle_authoring: {
        authoring_schema: "h3.context.nle_authoring_state.v1",
        profile_id: "h3.authoring.nle_content_extent.v1",
        prior_workspace_revision: 3,
        next_workspace_revision: 4,
        prior_workspace_fingerprint: fp,
        next_workspace_fingerprint: nextWorkspaceFingerprint,
        prior_timeline_revision: 2,
        next_timeline_revision: 2,
        prior_timeline_fingerprint: authoring.timeline_fingerprint,
        next_timeline_fingerprint: authoring.timeline_fingerprint,
        prior_authoring_fingerprint: fp,
        next_authoring_fingerprint: nextAuthoringFingerprint,
      },
      rows: [v1Row],
      authority_versions: [
        "h3.context.production_authoring_import.request.v2",
        "h3.context.production_authoring_import.receipt.v2",
        "h3.context.segment_artifact_receipt.v1",
        "h3.context.authoring_source.generated.v1",
        "h3.context.nle_authoring_state.v1",
        "h3.authoring.nle_content_extent.v1",
      ],
    },
    authoring_projection: authoringProjection,
    history_projection: {
      schema: "h3.context.timeline_history_projection.v2",
      workspace_handle: authoring.workspace_handle,
      authoring,
      render_snapshot: null,
      selection: [],
      undo_cursor: null,
      redo_cursor: null,
      rejection: null,
    },
  };
}
