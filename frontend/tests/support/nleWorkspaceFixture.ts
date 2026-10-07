// M25-16 test fixtures: the plan's `M25-16-overlay-v1-smoke` (120 s, 4 tracks, 64 clips) and
// `M25-16-overlay-v1-virtualized` (600 s, 8 tracks, 128 clips) snapshots, built from the
// accepted parity fixture so the output/capability blocks are the real accepted profile, plus
// content-free timeline history and authoring view-state builders.

import parityFixture from "../../../tests/fixtures/m25_16_scene_resolver_parity_v1.json";
import semanticCorpusFixture from "../../../tests/fixtures/m25_20_semantic_corpus_base_v1.json";

import type { AuthoringViewState } from "../../src/state/authoringViewState";
import type { TimelineCommandWire } from "../../src/contracts/authoringWorkbenchCodec";
import {
  decodeTimelineHistoryProjection,
  type TimelineHistoryProjection,
} from "../../src/contracts/authoringWorkbenchCodec";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
  type PublicCompositionSnapshot,
} from "../../src/contracts/compositionCodec";
import { projectionFixture } from "./authoringFixture";

type Wire = Record<string, unknown>;

export const FIXTURE_WORKSPACE_HANDLE = "authoring-" + "a".repeat(32);

function baseSnapshotWire(): Wire {
  // The parity fixture is imported (not read from disk) so the same builders serve the
  // browser harness under `e2e/` and the vitest suites.
  const parsed = parityFixture as unknown as { cases: { snapshot: Wire }[] };
  return structuredClone(parsed.cases[0]!.snapshot);
}

const IDENTITY_TRANSFORM = {
  anchor_x_bp: 5000,
  anchor_y_bp: 5000,
  position_x_bp: 0,
  position_y_bp: 0,
  scale_x_bp: 10000,
  scale_y_bp: 10000,
  rotation_mdeg: 0,
};

function mediaClip(
  clipId: string,
  assetId: string,
  trackId: string,
  startFrame: number,
  durationFrames: number,
): Wire {
  return {
    clip_id: clipId,
    asset_id: assetId,
    track_id: trackId,
    start_frame: startFrame,
    duration_frames: durationFrames,
    source_start_frame: 0,
    enabled: true,
    transform: IDENTITY_TRANSFORM,
    crop: { left_bp: 0, top_bp: 0, right_bp: 0, bottom_bp: 0 },
    opacity_bp: 10000,
    blend: "normal",
    text: null,
    transition: { kind: "none", duration_frames: 0 },
    effect: {
      kind: "none",
      brightness_permille: 0,
      contrast_permille: 1000,
      saturation_permille: 1000,
    },
  };
}

function titleClip(
  clipId: string,
  trackId: string,
  startFrame: number,
  durationFrames: number,
  fontAssetId = "h3.font.noto_sans.v1",
): Wire {
  return {
    ...mediaClip(
      clipId,
      "h3.font.noto_sans.v1",
      trackId,
      startFrame,
      durationFrames,
    ),
    asset_id: null,
    text: {
      content: "Title " + clipId,
      font_asset_id: fontAssetId,
      size_px: 48,
      weight: 400,
      style: "normal",
      align: "center",
      line_height_bp: 12000,
      fill_rgba: [255, 255, 255, 255],
      background_rgba: null,
    },
  };
}

export type FixtureShape = Readonly<{
  name: string;
  durationSeconds: number;
  trackKinds: readonly (
    "primary_video" | "video_overlay" | "image_overlay" | "text_overlay"
  )[];
  clipCount: number;
  /** Output geometry, when the shape needs one other than the parity fixture's 320 x 180. */
  output?: Readonly<{ width: number; height: number }>;
}>;

export const SMOKE_SHAPE: FixtureShape = Object.freeze({
  name: "M25-16-overlay-v1-smoke",
  durationSeconds: 120,
  trackKinds: [
    "primary_video",
    "video_overlay",
    "image_overlay",
    "text_overlay",
  ] as const,
  clipCount: 64,
});

/** M25-53 visual reference: three distinct, already-approved synthetic video sources. */
export const REFERENCE_SHAPE: FixtureShape = Object.freeze({
  name: "M25-53-reference-three-source-v1",
  durationSeconds: 6,
  trackKinds: ["primary_video"] as const,
  clipCount: 3,
});

/** Start before timeline assembly so the bin can acquire its asset-scoped posters. */
export const REFERENCE_PRELOAD_SHAPE: FixtureShape = Object.freeze({
  ...REFERENCE_SHAPE,
  name: "M25-53-reference-preload-v1",
  clipCount: 0,
});

/** M25-58 fixed-denominator product playback surface at the accepted output geometry. */
export const CONTINUOUS_PREVIEW_SHAPE: FixtureShape = Object.freeze({
  name: "M25-58-continuous-preview-v1",
  durationSeconds: 248 / 24,
  trackKinds: [
    "primary_video",
    "video_overlay",
    "image_overlay",
    "text_overlay",
  ] as const,
  clipCount: 6,
  output: Object.freeze({ width: 1920, height: 1080 }),
});

// Keep multilayer coverage independent from the ten-second stability fixture. Reusing its
// single-layer 248-frame input removes the image handoffs from the fixed 144-frame regression.
export const CONTINUOUS_MULTILAYER_PREVIEW_SHAPE: FixtureShape = Object.freeze({
  ...CONTINUOUS_PREVIEW_SHAPE,
  name: "continuous-preview-multilayer-v1",
  durationSeconds: 6,
});

const referenceClips = () => [
  mediaClip("reference-primary", "vid-primary", "track-primary", 0, 48),
  mediaClip("reference-overlay", "vid-overlay", "track-primary", 48, 24),
  mediaClip("reference-timing", "vid-timing", "track-primary", 72, 48),
];

export function referenceAssemblyCommands(): TimelineCommandWire[] {
  return referenceClips().map((clip) => ({
    kind: "insert_asset_clip",
    payload: { clip },
  }));
}

function referenceSnapshotWire(revision: number): Wire {
  const wire = structuredClone(
    (semanticCorpusFixture as unknown as { snapshot: Wire }).snapshot,
  );
  wire.workspace_handle = FIXTURE_WORKSPACE_HANDLE;
  wire.timeline_revision = revision;
  (wire.output as Wire).duration_frames = 144;
  wire.tracks = [
    {
      track_id: "track-primary",
      kind: "primary_video",
      order: 0,
      enabled: true,
      locked: false,
    },
  ];
  wire.clips = referenceClips();
  wire.blockers = [];
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return wire;
}

function continuousPreviewSnapshotWire(
  revision: number,
  multilayer = false,
): Wire {
  const wire = baseSnapshotWire();
  const output = wire.output as Wire;
  output.duration_frames = multilayer ? 144 : 248;
  output.width = 1920;
  output.height = 1080;
  wire.workspace_handle = FIXTURE_WORKSPACE_HANDLE;
  wire.timeline_revision = revision;
  wire.tracks = CONTINUOUS_PREVIEW_SHAPE.trackKinds.map((kind, order) => ({
    track_id: `track-${order}`,
    kind,
    order,
    enabled: true,
    locked: false,
  }));
  const primaryClips = [
    mediaClip("preview-primary-0", "vid-primary", "track-0", 0, 48),
    mediaClip("preview-primary-1", "vid-primary", "track-0", 48, 48),
    mediaClip("preview-primary-2", "vid-primary", "track-0", 96, 48),
  ];
  wire.clips = multilayer
    ? [
        ...primaryClips,
        mediaClip("preview-image-0", "img-overlay", "track-2", 24, 24),
        mediaClip("preview-image-1", "img-overlay", "track-2", 72, 24),
        mediaClip("preview-image-2", "img-overlay", "track-2", 120, 24),
      ]
    : [
        ...primaryClips,
        mediaClip("preview-primary-3", "vid-primary", "track-0", 144, 48),
        mediaClip("preview-primary-4", "vid-primary", "track-0", 192, 48),
        mediaClip("preview-primary-5", "vid-primary", "track-0", 240, 8),
      ];
  wire.blockers = [];
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return wire;
}

export const VIRTUALIZED_SHAPE: FixtureShape = Object.freeze({
  name: "M25-16-overlay-v1-virtualized",
  durationSeconds: 600,
  trackKinds: [
    "primary_video",
    "video_overlay",
    "image_overlay",
    "image_overlay",
    "image_overlay",
    "image_overlay",
    "text_overlay",
    "text_overlay",
  ] as const,
  clipCount: 128,
});

/**
 * M25-45 AC45-02: the smoke shape turned on its side, 180 x 320 instead of 320 x 180.
 *
 * The monitor's fit rule is width-limited for every landscape composition in the default layout,
 * so a portrait one is the only way to reach the height-limited branch without first dragging the
 * layout into a shape no user starts from. The pixel count is the smoke shape's, so nothing about
 * decode cost or the composition contract's ceiling changes -- only the aspect.
 */
export const PORTRAIT_SHAPE: FixtureShape = Object.freeze({
  ...SMOKE_SHAPE,
  name: "M25-45-portrait-v1-smoke",
  output: Object.freeze({ width: 180, height: 320 }),
});

/**
 * M25-21 `NLE-STRESS-V1`: the frozen 600 s / 8-track / 128-clip editing and preview stress
 * fixture. Its geometry is the virtualized shape's; the name is the fixture identity the hardening
 * report binds its measurements to, so the two must never be edited apart.
 */
export const NLE_STRESS_SHAPE: FixtureShape = Object.freeze({
  ...VIRTUALIZED_SHAPE,
  name: "NLE-STRESS-V1",
});

/** Build a decodable snapshot wire for a shape; clips are distributed round-robin over tracks. */
export function snapshotWire(shape: FixtureShape, revision = 11): Wire {
  if (shape.name === CONTINUOUS_MULTILAYER_PREVIEW_SHAPE.name)
    return continuousPreviewSnapshotWire(revision, true);
  if (shape.name === CONTINUOUS_PREVIEW_SHAPE.name)
    return continuousPreviewSnapshotWire(revision);
  if (shape.name === REFERENCE_SHAPE.name)
    return referenceSnapshotWire(revision);
  if (shape.name === REFERENCE_PRELOAD_SHAPE.name) {
    const wire = referenceSnapshotWire(revision);
    wire.clips = [];
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    return wire;
  }
  const wire = baseSnapshotWire();
  const output = wire.output as Wire;
  const durationFrames = shape.durationSeconds * 24;
  output.duration_frames = durationFrames;
  if (shape.output !== undefined) {
    output.width = shape.output.width;
    output.height = shape.output.height;
  }
  wire.workspace_handle = FIXTURE_WORKSPACE_HANDLE;
  wire.timeline_revision = revision;
  const videoAsset = (wire.assets as Wire[]).find(
    (asset) => asset.kind === "video",
  )!;
  if (shape.name === NLE_STRESS_SHAPE.name) {
    // IMPORTANT: exact playback needs the pinned generic CFR source's real frame timing.
    // The sparse parity landmarks otherwise resolve a seek four frames into a clip to PTS 0.
    // This 48-frame/96,000-sample descriptor is for cfr-primary.mp4 only, never user media.
    videoAsset.source_frame_count = 48;
    videoAsset.source_sample_count = 96_000;
    videoAsset.landmarks = Array.from({ length: 48 }, (_, frameIndex) => ({
      frame_index: frameIndex,
      pts: frameIndex * 512,
      dts: frameIndex * 512,
      duration_ticks: 512,
    }));
  }
  wire.assets = [
    videoAsset,
    {
      asset_id: "img-still",
      kind: "image",
      source_time_base: null,
      source_frame_count: null,
      source_sample_count: null,
      embedded_audio: "absent",
      timestamp_policy: "not_applicable",
      landmarks: [],
    },
    {
      asset_id: "h3.font.noto_sans.v1",
      kind: "font",
      source_time_base: null,
      source_frame_count: null,
      source_sample_count: null,
      embedded_audio: "absent",
      timestamp_policy: "not_applicable",
      landmarks: [],
    },
  ];
  wire.tracks = shape.trackKinds.map((kind, order) => ({
    track_id: `track-${order}`,
    kind,
    order,
    enabled: true,
    locked: false,
  }));
  const perTrack = Math.ceil(shape.clipCount / shape.trackKinds.length);
  const slot = Math.floor(durationFrames / perTrack);
  const clips: Wire[] = [];
  let index = 0;
  for (
    let position = 0;
    position < perTrack && clips.length < shape.clipCount;
    position += 1
  ) {
    for (
      let trackIndex = 0;
      trackIndex < shape.trackKinds.length;
      trackIndex += 1
    ) {
      if (clips.length >= shape.clipCount) break;
      const kind = shape.trackKinds[trackIndex]!;
      const trackId = `track-${trackIndex}`;
      // The accepted compositor admits two concurrent still owners. Stagger the
      // extra image tracks without reducing the frozen 8-track/128-clip fixture.
      const start =
        position * slot +
        (kind === "image_overlay" && trackIndex >= 4 ? 48 : 0);
      // Leave a gap so trims can extend; keep clips inside the output duration.
      const duration = Math.max(2, Math.min(48, slot - 2));
      const clipId = `clip-${index}`;
      index += 1;
      if (kind === "text_overlay")
        clips.push(titleClip(clipId, trackId, start, duration));
      else if (kind === "image_overlay")
        clips.push(mediaClip(clipId, "img-still", trackId, start, duration));
      else
        clips.push(
          mediaClip(
            clipId,
            String(videoAsset.asset_id),
            trackId,
            start,
            duration,
          ),
        );
    }
  }
  wire.clips = clips;
  wire.blockers = [];
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return wire;
}

export function snapshotFixture(
  shape: FixtureShape,
  revision = 11,
): PublicCompositionSnapshot {
  return decodePublicCompositionSnapshot(snapshotWire(shape, revision));
}

export function historyWire(
  shape: FixtureShape,
  overrides: Partial<{
    selection: string[];
    undo_cursor: string | null;
    redo_cursor: string | null;
    rejection: { code: string } | null;
    revision: number;
  }> = {},
): Wire {
  const revision = overrides.revision ?? 11;
  return {
    schema: "h3.context.timeline_history_projection.v1",
    workspace_handle: FIXTURE_WORKSPACE_HANDLE,
    snapshot: snapshotWire(shape, revision),
    selection: overrides.selection ?? [],
    undo_cursor:
      overrides.undo_cursor === undefined
        ? `h3.context.timeline_history_cursor.v1:${revision}:${"a".repeat(64)}`
        : overrides.undo_cursor,
    redo_cursor:
      overrides.redo_cursor === undefined ? null : overrides.redo_cursor,
    rejection: overrides.rejection ?? null,
  };
}

export function historyFixture(
  shape: FixtureShape,
  overrides: Parameters<typeof historyWire>[1] = {},
): TimelineHistoryProjection {
  return decodeTimelineHistoryProjection(historyWire(shape, overrides));
}

export function authoringReady(
  shape: FixtureShape,
  overrides: Parameters<typeof historyWire>[1] = {},
): AuthoringViewState {
  return {
    status: "ready",
    projection: projectionFixture({
      workspace_handle: FIXTURE_WORKSPACE_HANDLE,
    }),
    timelineHistory: historyFixture(shape, overrides),
  };
}
