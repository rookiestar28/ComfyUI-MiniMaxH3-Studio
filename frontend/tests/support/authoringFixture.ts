// M20-03 test fixture: raw wire builders run through the real decoder, so every
// test that uses them also proves wire validity (house convention).

import {
  decodeAuthoringProjection,
  type AuthoringProjection,
} from "../../src/contracts/authoringWorkbenchCodec";

export const FP = "sha256:" + "0".repeat(64);

export function capacityWire(): Record<string, unknown> {
  return {
    schema: "h3-context-reference-set-authoring/1",
    aggregate_max: 12,
    aggregate_used: 3,
    aggregate_remaining: 9,
    image_used: 1,
    image_remaining: 8,
    video_used: 1,
    video_remaining: 2,
    paired_audio_used: 0,
    paired_audio_remaining: 3,
    standalone_audio_used: 1,
    standalone_audio_remaining: 2,
    timed: { max_duration_milliseconds: 149_687, max_frames: 3_600 },
  };
}

export function sourceWire(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    source_id: "vid-1",
    kind: "video",
    admitted: true,
    admissible: true,
    reason: null,
    duration_milliseconds: 5_000,
    label: "<Video 1>",
    paired_with: null,
    derived_soundtrack: null,
    preview: {
      schema: "h3.context.authoring_source_preview.capability.v1",
      available: false,
      reason: "not_bound",
    },
    ...overrides,
  };
}

export function clipWire(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    clip_id: "clip-1",
    asset_id: "vid-1",
    kind: "video",
    lane: 0,
    start_frame: 0,
    frames: 12,
    source_start_frame: 0,
    envelope: [],
    ...overrides,
  };
}

export function projectionWire(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schema: "h3.context.authoring_workbench.projection.v1",
    workspace_handle: "authoring-abc123",
    context_source_id: "report-1",
    task_mode: "t2va",
    registry_fingerprint: FP,
    reference: {
      revision: 4,
      sources: [
        sourceWire({
          source_id: "img-1",
          kind: "image",
          duration_milliseconds: null,
          label: "<Picture 1>",
        }),
        sourceWire({ derived_soundtrack: "blocked_unknown" }),
        sourceWire({
          source_id: "aud-1",
          kind: "audio",
          label: null,
          derived_soundtrack: null,
        }),
        sourceWire({
          source_id: "aud-2",
          kind: "audio",
          label: "<Audio 1>",
          derived_soundtrack: null,
        }),
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
          source_id: "aud-2",
          kind: "audio",
          label: "<Audio 1>",
          paired_with: null,
        },
      ],
      soundtracks: [
        {
          video_id: "vid-1",
          derived_state: "blocked_unknown",
          soundtrack_source_id: "aud-1",
        },
      ],
      queue_blockers: [{ video_id: "vid-1", code: "blocked_unknown" }],
      capacity: capacityWire(),
    },
    availability: { producer: null, revision: 0 },
    timeline: {
      revision: 2,
      content_fingerprint: FP,
      profile: {
        video_fps: 24,
        frame_grid: 51,
        audio_period_frames: 3,
        max_extent_frames: 3_600,
      },
      clips: [clipWire()],
      links: [],
      selection: [],
      blockers: [],
    },
    rejection: null,
    ...overrides,
  };
}

export function projectionFixture(
  overrides: Record<string, unknown> = {},
): AuthoringProjection {
  return decodeAuthoringProjection(projectionWire(overrides));
}

export function snapWire(): Record<string, unknown> {
  return {
    schema: "h3.context.authoring_workbench.snap.v1",
    workspace_handle: "authoring-abc123",
    timeline_revision: 2,
    candidates: [
      { frame: 51, kind: "grid", distance: 1 },
      { frame: 48, kind: "clip_boundary", distance: 2 },
    ],
  };
}
