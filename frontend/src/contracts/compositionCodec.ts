import { sha256Text } from "./canonicalFingerprint";
import {
  audioExtensionKeys,
  capabilityProfileKeys,
  clipAudioKeys,
  compositionBlockerKeys,
  compositionClipKeys,
  compositionTrackKeys,
  cropKeys,
  effectKeys,
  embeddedAudioSpanKeys,
  outputProfileKeys,
  publicAssetKeys,
  publicCompositionSnapshotKeys,
  rationalKeys,
  renderVocabularyKeys,
  resolvedLayerKeys,
  resolvedSceneKeys,
  textStyleKeys,
  timingLandmarkKeys,
  transform2DKeys,
  transitionKeys,
} from "./generatedSurface";

export const PUBLIC_SNAPSHOT_SCHEMA =
  "h3.context.public_composition_snapshot.v1" as const;
export const RESOLVED_SCENE_SCHEMA = "h3.context.resolved_scene.v1" as const;
export const ENGINE_PROFILE_ID = "h3.native_media_canvas_backend.v1" as const;
export const OPERATION_PROFILE_ID = "h3.authoring.nle_operation.v1" as const;
export const OUTPUT_PROFILE_ID =
  "h3.authoring.output.h264_aac_24fps.v1" as const;
export const AUDIO_EXTENSION_SCHEMA =
  "h3.authoring.independent_audio_extension.v1" as const;
export const INDEPENDENT_AUDIO_COMMAND_NAMESPACE =
  "h3.authoring.audio.command.v1" as const;

export const BLOCKER_CODES = Object.freeze([
  "unsupported_profile",
  "unsupported_output_profile",
  "operation_not_in_profile",
  "audio_editing_deferred",
  "private_field",
  "negative_timestamp",
  "invalid_timing",
  "timing_unavailable",
  "source_range_unavailable",
  "embedded_audio_unavailable",
  "stale_snapshot",
  "resource_limit",
  "invalid_contract",
] as const);

// `set_clip_audio` is a video clip's own gain, mute and fades. It is not an independent-audio
// command: those names, and the reserved namespace, stay refused as `audio_editing_deferred`.
// Keep comments out of this literal: `generatedSurface.test.ts` reads it as an all-literal array.
export const NLE_OPERATION_IDS = Object.freeze([
  "create_track",
  "remove_track",
  "reorder_track",
  "set_track_enabled",
  "set_track_locked",
  "insert_asset_clip",
  "insert_title_clip",
  "replace_clip_asset",
  "remove_clip",
  "move_clip",
  "move_group",
  "trim_clip",
  "split_clip",
  "merge_clips",
  "insert_range",
  "overwrite_range",
  "ripple_delete",
  "ripple_trim",
  "roll_edit",
  "slip_clip",
  "slide_clip",
  "set_clip_enabled",
  "set_visual_transform",
  "set_crop",
  "set_opacity_blend",
  "set_text_content",
  "set_text_style",
  "set_transition",
  "set_effect",
  "set_clip_audio",
  "select_clips",
  "undo",
  "redo",
  "rebase_transaction",
] as const);

export const RENDER_JOB_STATES = Object.freeze([
  "queued",
  "probing",
  "preparing",
  "rendering",
  "encoding",
  "muxing",
  "validating",
  "succeeded",
  "failed",
  "cancelled",
] as const);
export const RENDER_TERMINAL_STATES = Object.freeze([
  "succeeded",
  "failed",
  "cancelled",
] as const);
export const RENDER_TERMINAL_REASONS = Object.freeze([
  "unsupported_profile",
  "source_unavailable",
  "source_replaced",
  "stale_snapshot",
  "cancelled",
  "timeout",
  "renderer_failed",
  "validation_failed",
  "internal_failure",
] as const);

const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const canonicalKey = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const privateFields = new Set([
  "private_source_manifest",
  "derivative_manifest",
  "runtime_identity",
  "source_path",
  "source_url",
  "blob_url",
  "lease",
  "credential",
]);
const MAX_SAFE = Number.MAX_SAFE_INTEGER;

type Rational = Readonly<{ num: number; den: number }>;
type TimingLandmark = Readonly<{
  frameIndex: number;
  pts: number;
  dts: number;
  durationTicks: number;
}>;
export type PublicCompositionAsset = Readonly<{
  assetId: string;
  kind: "video" | "image" | "font";
  sourceTimeBase: Rational | null;
  sourceFrameCount: number | null;
  sourceSampleCount: number | null;
  embeddedAudio:
    "present_bound" | "absent" | "unavailable" | "excluded_overlay_policy";
  timestampPolicy: "nonnegative_monotonic_v1" | "not_applicable";
  landmarks: readonly TimingLandmark[];
}>;
export type CompositionTrack = Readonly<{
  trackId: string;
  kind: "primary_video" | "video_overlay" | "image_overlay" | "text_overlay";
  order: number;
  enabled: boolean;
  locked: boolean;
}>;
/** Gain (millibels), mute and fades (output frames) of a video clip's embedded audio. */
export type ClipAudio = Readonly<{
  gainMb: number;
  muted: boolean;
  fadeInFrames: number;
  fadeOutFrames: number;
}>;
export const CLIP_AUDIO_GAIN_MIN_MB = -6_000;
export const CLIP_AUDIO_GAIN_MAX_MB = 1_200;
export const CLIP_AUDIO_FADE_MAX_FRAMES = 240;
/** The value a clip without the wire member has; it is never written on the wire. */
export const IDENTITY_CLIP_AUDIO: ClipAudio = Object.freeze({
  gainMb: 0,
  muted: false,
  fadeInFrames: 0,
  fadeOutFrames: 0,
});
export type CompositionClip = Readonly<{
  clipId: string;
  assetId: string | null;
  trackId: string;
  startFrame: number;
  durationFrames: number;
  sourceStartFrame: number;
  enabled: boolean;
  transform: Readonly<Record<string, number>>;
  crop: Readonly<Record<string, number>>;
  opacityBp: number;
  blend: "normal" | "multiply" | "screen";
  text: Readonly<Record<string, unknown>> | null;
  transition: Readonly<{ kind: string; durationFrames: number }>;
  effect: Readonly<Record<string, number | string>>;
  audio: ClipAudio;
}>;
export type PublicCompositionSnapshot = Readonly<{
  schema: typeof PUBLIC_SNAPSHOT_SCHEMA;
  profileId: typeof ENGINE_PROFILE_ID;
  operationProfileId: typeof OPERATION_PROFILE_ID;
  projectId: string;
  workspaceHandle: string;
  workspaceRevision: number;
  workspaceFingerprint: string;
  timelineRevision: number;
  timelineFingerprint: string;
  publicFingerprint: string;
  output: Readonly<Record<string, unknown>>;
  capability: Readonly<Record<string, unknown>>;
  assets: readonly PublicCompositionAsset[];
  tracks: readonly CompositionTrack[];
  clips: readonly CompositionClip[];
  audioExtension: Readonly<Record<string, unknown>>;
  blockers: readonly Readonly<{ code: string; subjectId: string | null }>[];
  renderVocabulary: Readonly<Record<string, unknown>>;
}>;
export type ResolvedCompositionScene = Readonly<{
  schema: typeof RESOLVED_SCENE_SCHEMA;
  profileId: typeof ENGINE_PROFILE_ID;
  publicFingerprint: string;
  frame: number;
  layers: readonly Readonly<Record<string, unknown>>[];
  audioSpan: Readonly<Record<string, number | string>> | null;
  blockers: readonly Readonly<{ code: string; subjectId: string | null }>[];
}>;

export class CompositionContractError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    // CRITICAL: an out-of-vocabulary code collapses to `invalid_contract`, mirroring
    // `composition_contract.CompositionContractError`. Without it `.code` could carry a value the
    // shared blocker vocabulary does not contain, and a reader that switches on the code would
    // meet a case it cannot have. No call site in this file passes an unlisted code today, so this
    // is the guard for the one a future edit adds -- and the constructor is the only place both
    // implementations can enforce it.
    const listed = (BLOCKER_CODES as readonly string[]).includes(code)
      ? code
      : "invalid_contract";
    super(`${listed}: ${message}`);
    this.name = "CompositionContractError";
    this.code = listed;
  }
}

function fail(code: string, message: string): never {
  // IMPORTANT: cross-language parity consumes the typed code only. Do not make callers parse
  // diagnostic prose; message wording is intentionally free to change between implementations.
  throw new CompositionContractError(code, message);
}

function object(
  value: unknown,
  keys: readonly string[],
  name: string,
): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    fail("invalid_contract", `${name} must be an object`);
  const wire = value as Record<string, unknown>;
  const actual = Object.keys(wire);
  if (actual.some((key) => privateFields.has(key)))
    fail("private_field", `${name} contains a reserved private field`);
  if (JSON.stringify([...actual].sort()) !== JSON.stringify([...keys].sort()))
    fail("invalid_contract", `${name} must be closed`);
  return wire;
}

function exactInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < minimum ||
    (value as number) > maximum
  )
    fail("invalid_contract", `${name} is outside its closed bounds`);
  return value as number;
}

function resourceArray(
  value: unknown,
  maximum: number,
  name: string,
): readonly unknown[] {
  if (!Array.isArray(value))
    fail("invalid_contract", `${name} must be an array`);
  if (value.length > maximum)
    fail("resource_limit", `${name} exceeds the resource profile`);
  return value;
}

function exactIdentifier(value: unknown, name: string): string {
  if (typeof value !== "string" || !identifier.test(value))
    fail("invalid_contract", `${name} must be a bounded identifier`);
  return value;
}

function exactFingerprint(value: unknown, name: string): string {
  if (typeof value !== "string" || !fingerprint.test(value))
    fail("invalid_contract", `${name} must be a sha256 fingerprint`);
  return value;
}

function exactBoolean(value: unknown, name: string): boolean {
  if (typeof value !== "boolean")
    fail("invalid_contract", `${name} must be a boolean`);
  return value;
}

function oneOf<T extends string>(
  value: unknown,
  values: readonly T[],
  name: string,
): T {
  if (typeof value !== "string" || !values.includes(value as T))
    fail("invalid_contract", `${name} is outside the closed vocabulary`);
  return value as T;
}

function rational(
  value: unknown,
  name: string,
  maximumNumerator = 240_000,
  maximumDenominator = 1_001,
): Rational {
  const wire = object(value, rationalKeys, name);
  const num = exactInteger(wire.num, 1, maximumNumerator, `${name}.num`);
  const den = exactInteger(wire.den, 1, maximumDenominator, `${name}.den`);
  let a = num;
  let b = den;
  while (b !== 0) [a, b] = [b, a % b];
  if (a !== 1) fail("invalid_timing", `${name} must be reduced`);
  return Object.freeze({ num, den });
}

function sourceClockRational(value: unknown, name: string): Rational {
  // IMPORTANT: source clocks are observed media facts, not output-rate controls. Keep their
  // exact-integer bound separate or the accepted 1/12288 M25-09 corpus is rejected as invalid.
  return rational(value, name, MAX_SAFE, MAX_SAFE);
}

function decodeLandmarks(
  value: unknown,
  name: string,
): readonly TimingLandmark[] {
  // CRITICAL: Production admits up to 512 frames per segment. Narrowing this mirror makes a valid
  // generated output impossible to import even though the independent snapshot total stays 2,048.
  const rows = resourceArray(value, 512, name);
  let previousFrame = -1;
  let previousPts = -1;
  let previousDts = -1;
  return Object.freeze(
    rows.map((row, index) => {
      const rowName = `${name}[${index}]`;
      const wire = object(row, timingLandmarkKeys, rowName);
      for (const member of ["frame_index", "pts", "dts"] as const) {
        if (
          typeof wire[member] === "number" &&
          Number.isInteger(wire[member]) &&
          wire[member] < 0
        )
          fail("negative_timestamp", `${rowName}.${member} cannot be negative`);
      }
      const frameIndex = exactInteger(
        wire.frame_index,
        0,
        1_000_000,
        `${rowName}.frame_index`,
      );
      const pts = exactInteger(wire.pts, 0, MAX_SAFE, `${rowName}.pts`);
      const dts = exactInteger(wire.dts, 0, MAX_SAFE, `${rowName}.dts`);
      const durationTicks = exactInteger(
        wire.duration_ticks,
        1,
        MAX_SAFE,
        `${rowName}.duration_ticks`,
      );
      if (index === 0 && (frameIndex !== 0 || pts !== 0))
        fail("invalid_timing", `${name} must begin at frame and PTS zero`);
      if (
        frameIndex <= previousFrame ||
        pts <= previousPts ||
        dts < previousDts
      )
        fail("invalid_timing", `${name} must be monotonic`);
      previousFrame = frameIndex;
      previousPts = pts;
      previousDts = dts;
      return Object.freeze({ frameIndex, pts, dts, durationTicks });
    }),
  );
}

function decodeAsset(value: unknown, index: number): PublicCompositionAsset {
  const name = `assets[${index}]`;
  const wire = object(value, publicAssetKeys, name);
  const kind = oneOf(
    wire.kind,
    ["video", "image", "font"] as const,
    `${name}.kind`,
  );
  const landmarks = decodeLandmarks(wire.landmarks, `${name}.landmarks`);
  let sourceTimeBase: Rational | null = null;
  let sourceFrameCount: number | null = null;
  let sourceSampleCount: number | null = null;
  let embeddedAudio: PublicCompositionAsset["embeddedAudio"];
  let timestampPolicy: PublicCompositionAsset["timestampPolicy"];
  if (kind === "video") {
    if (wire.source_time_base === null)
      fail("invalid_timing", `${name} video requires a source timebase`);
    sourceTimeBase = sourceClockRational(
      wire.source_time_base,
      `${name}.source_time_base`,
    );
    const admittedFrameCount = exactInteger(
      wire.source_frame_count,
      1,
      1_000_000,
      `${name}.source_frame_count`,
    );
    sourceFrameCount = admittedFrameCount;
    sourceSampleCount =
      wire.source_sample_count === null
        ? null
        : exactInteger(
            wire.source_sample_count,
            1,
            MAX_SAFE,
            `${name}.source_sample_count`,
          );
    embeddedAudio = oneOf(
      wire.embedded_audio,
      [
        "present_bound",
        "absent",
        "unavailable",
        "excluded_overlay_policy",
      ] as const,
      `${name}.embedded_audio`,
    );
    if (embeddedAudio === "present_bound" && sourceSampleCount === null)
      fail("invalid_timing", `${name} bound audio requires a sample count`);
    if (landmarks.some((landmark) => landmark.frameIndex >= admittedFrameCount))
      fail(
        "invalid_timing",
        `${name} landmark exceeds the admitted frame count`,
      );
    timestampPolicy = oneOf(
      wire.timestamp_policy,
      ["nonnegative_monotonic_v1"] as const,
      `${name}.timestamp_policy`,
    );
  } else {
    if (
      wire.source_time_base !== null ||
      wire.source_frame_count !== null ||
      wire.source_sample_count !== null ||
      landmarks.length !== 0
    )
      fail("invalid_timing", `${name} untimed asset carries timing`);
    if (
      wire.embedded_audio !== "absent" ||
      wire.timestamp_policy !== "not_applicable"
    )
      fail("invalid_contract", `${name} untimed capability is invalid`);
    embeddedAudio = "absent";
    timestampPolicy = "not_applicable";
  }
  return Object.freeze({
    assetId: exactIdentifier(wire.asset_id, `${name}.asset_id`),
    kind,
    sourceTimeBase,
    sourceFrameCount,
    sourceSampleCount,
    embeddedAudio,
    timestampPolicy,
    landmarks,
  });
}

/** Decode one asset with the same closed shape used by public snapshots. */
export function decodePublicCompositionAsset(
  value: unknown,
  index = 0,
): PublicCompositionAsset {
  return decodeAsset(value, index);
}

function decodeTrack(value: unknown, index: number): CompositionTrack {
  const name = `tracks[${index}]`;
  const wire = object(value, compositionTrackKeys, name);
  return Object.freeze({
    trackId: exactIdentifier(wire.track_id, `${name}.track_id`),
    kind: oneOf(
      wire.kind,
      [
        "primary_video",
        "video_overlay",
        "image_overlay",
        "text_overlay",
      ] as const,
      `${name}.kind`,
    ),
    order: exactInteger(wire.order, 0, 7, `${name}.order`),
    enabled: exactBoolean(wire.enabled, `${name}.enabled`),
    locked: exactBoolean(wire.locked, `${name}.locked`),
  });
}

/** Decode one track with the same closed shape used by public snapshots. */
export function decodeCompositionTrack(
  value: unknown,
  index = 0,
): CompositionTrack {
  return decodeTrack(value, index);
}

function numericObject(
  value: unknown,
  keys: readonly string[],
  bounds: Readonly<Record<string, readonly [number, number]>>,
  name: string,
): Readonly<Record<string, number>> {
  const wire = object(value, keys, name);
  return Object.freeze(
    Object.fromEntries(
      Object.entries(bounds).map(([key, [minimum, maximum]]) => [
        key,
        exactInteger(wire[key], minimum, maximum, `${name}.${key}`),
      ]),
    ),
  );
}

function rgba(value: unknown, name: string): readonly number[] {
  if (!Array.isArray(value) || value.length !== 4)
    fail("invalid_contract", `${name} must be RGBA8`);
  return Object.freeze(
    value.map((member, index) =>
      exactInteger(member, 0, 255, `${name}[${index}]`),
    ),
  );
}

function decodeText(
  value: unknown,
  name: string,
): Readonly<Record<string, unknown>> | null {
  if (value === null) return null;
  const wire = object(value, textStyleKeys, name);
  if (
    typeof wire.content !== "string" ||
    Array.from(wire.content).length < 1 ||
    Array.from(wire.content).length > 2_048 ||
    wire.content.split(/\r?\n/u).length > 32 ||
    /\u0000|[\u0001-\u0008\u000B\u000C\u000E-\u001F]/u.test(wire.content)
  )
    fail("invalid_contract", `${name}.content is outside text bounds`);
  if (wire.content.normalize("NFC") !== wire.content)
    fail("invalid_contract", `${name}.content must already be NFC`);
  const weight = exactInteger(wire.weight, 400, 700, `${name}.weight`);
  if (weight !== 400 && weight !== 700)
    fail("invalid_contract", `${name}.weight is unsupported`);
  return Object.freeze({
    content: wire.content.normalize("NFC"),
    fontAssetId: exactIdentifier(wire.font_asset_id, `${name}.font_asset_id`),
    sizePx: exactInteger(wire.size_px, 8, 512, `${name}.size_px`),
    weight,
    style: oneOf(wire.style, ["normal", "italic"] as const, `${name}.style`),
    align: oneOf(
      wire.align,
      ["left", "center", "right"] as const,
      `${name}.align`,
    ),
    lineHeightBp: exactInteger(
      wire.line_height_bp,
      7_500,
      30_000,
      `${name}.line_height_bp`,
    ),
    fillRgba: rgba(wire.fill_rgba, `${name}.fill_rgba`),
    backgroundRgba:
      wire.background_rgba === null
        ? null
        : rgba(wire.background_rgba, `${name}.background_rgba`),
  });
}

export function isIdentityClipAudio(audio: ClipAudio): boolean {
  return (
    audio.gainMb === 0 &&
    !audio.muted &&
    audio.fadeInFrames === 0 &&
    audio.fadeOutFrames === 0
  );
}

/** Decode a clip wire's `audio` member (`composition_contract._clip_audio`). */
export function decodeClipAudio(
  value: unknown,
  durationFrames: number,
  name: string,
): ClipAudio {
  const wire = object(value, clipAudioKeys, name);
  const audio = Object.freeze({
    gainMb: exactInteger(
      wire.gain_mb,
      CLIP_AUDIO_GAIN_MIN_MB,
      CLIP_AUDIO_GAIN_MAX_MB,
      `${name}.gain_mb`,
    ),
    muted: exactBoolean(wire.muted, `${name}.muted`),
    fadeInFrames: exactInteger(
      wire.fade_in_frames,
      0,
      CLIP_AUDIO_FADE_MAX_FRAMES,
      `${name}.fade_in_frames`,
    ),
    fadeOutFrames: exactInteger(
      wire.fade_out_frames,
      0,
      CLIP_AUDIO_FADE_MAX_FRAMES,
      `${name}.fade_out_frames`,
    ),
  });
  if (audio.fadeInFrames + audio.fadeOutFrames > durationFrames)
    fail("invalid_contract", `${name} fades exceed the clip duration`);
  // CRITICAL: one value, one wire. Admitting the identity written out gives a clip two encodings
  // and two public fingerprints for the same edit.
  if (isIdentityClipAudio(audio))
    fail("invalid_contract", `${name} identity must be omitted`);
  return audio;
}

const clipKeysWithoutAudio = compositionClipKeys.filter(
  (key) => key !== "audio",
);

/**
 * The closed key set a clip wire must have: the generated list holds every key a clip may carry,
 * and `audio` is the one that may be absent (absence is the identity value).
 */
export function compositionClipWireKeys(value: unknown): readonly string[] {
  return value !== null &&
    typeof value === "object" &&
    Object.hasOwn(value, "audio")
    ? compositionClipKeys
    : clipKeysWithoutAudio;
}

function decodeClip(value: unknown, index: number): CompositionClip {
  const name = `clips[${index}]`;
  const wire = object(value, compositionClipWireKeys(value), name);
  const durationFrames = exactInteger(
    wire.duration_frames,
    1,
    1_000_000,
    `${name}.duration_frames`,
  );
  const transform = numericObject(
    wire.transform,
    transform2DKeys,
    {
      anchor_x_bp: [0, 10_000],
      anchor_y_bp: [0, 10_000],
      position_x_bp: [-40_000, 40_000],
      position_y_bp: [-40_000, 40_000],
      scale_x_bp: [1, 80_000],
      scale_y_bp: [1, 80_000],
      rotation_mdeg: [-180_000, 180_000],
    },
    `${name}.transform`,
  );
  const crop = numericObject(
    wire.crop,
    cropKeys,
    {
      left_bp: [0, 9_999],
      top_bp: [0, 9_999],
      right_bp: [0, 9_999],
      bottom_bp: [0, 9_999],
    },
    `${name}.crop`,
  );
  if (
    (crop.left_bp ?? 0) + (crop.right_bp ?? 0) >= 10_000 ||
    (crop.top_bp ?? 0) + (crop.bottom_bp ?? 0) >= 10_000
  )
    fail("invalid_contract", `${name}.crop removes the full image`);
  const transitionWire = object(
    wire.transition,
    transitionKeys,
    `${name}.transition`,
  );
  const transitionKind = oneOf(
    transitionWire.kind,
    ["none", "cross_dissolve_v1"] as const,
    `${name}.transition.kind`,
  );
  const transitionDuration = exactInteger(
    transitionWire.duration_frames,
    0,
    300,
    `${name}.transition.duration_frames`,
  );
  if (
    (transitionKind === "none" && transitionDuration !== 0) ||
    (transitionKind === "cross_dissolve_v1" &&
      (transitionDuration < 1 || transitionDuration > durationFrames))
  )
    fail("invalid_contract", `${name}.transition duration is invalid`);
  const effectWire = object(wire.effect, effectKeys, `${name}.effect`);
  const effect = Object.freeze({
    kind: oneOf(
      effectWire.kind,
      ["none", "color_adjust_v1"] as const,
      `${name}.effect.kind`,
    ),
    brightnessPermille: exactInteger(
      effectWire.brightness_permille,
      -1_000,
      1_000,
      `${name}.effect.brightness`,
    ),
    contrastPermille: exactInteger(
      effectWire.contrast_permille,
      0,
      2_000,
      `${name}.effect.contrast`,
    ),
    saturationPermille: exactInteger(
      effectWire.saturation_permille,
      0,
      2_000,
      `${name}.effect.saturation`,
    ),
  });
  if (
    effect.kind === "none" &&
    (effect.brightnessPermille !== 0 ||
      effect.contrastPermille !== 1_000 ||
      effect.saturationPermille !== 1_000)
  )
    fail("invalid_contract", `${name}.effect none must use identity values`);
  return Object.freeze({
    clipId: exactIdentifier(wire.clip_id, `${name}.clip_id`),
    assetId:
      wire.asset_id === null
        ? null
        : exactIdentifier(wire.asset_id, `${name}.asset_id`),
    trackId: exactIdentifier(wire.track_id, `${name}.track_id`),
    startFrame: exactInteger(
      wire.start_frame,
      0,
      999_999,
      `${name}.start_frame`,
    ),
    durationFrames,
    sourceStartFrame: exactInteger(
      wire.source_start_frame,
      0,
      999_999,
      `${name}.source_start_frame`,
    ),
    enabled: exactBoolean(wire.enabled, `${name}.enabled`),
    transform,
    crop,
    opacityBp: exactInteger(wire.opacity_bp, 0, 10_000, `${name}.opacity_bp`),
    blend: oneOf(
      wire.blend,
      ["normal", "multiply", "screen"] as const,
      `${name}.blend`,
    ),
    text: decodeText(wire.text, `${name}.text`),
    transition: Object.freeze({
      kind: transitionKind,
      durationFrames: transitionDuration,
    }),
    effect,
    audio: Object.hasOwn(wire, "audio")
      ? decodeClipAudio(wire.audio, durationFrames, `${name}.audio`)
      : IDENTITY_CLIP_AUDIO,
  });
}

/** Decode one clip with the same closed shape used by public snapshots. */
export function decodeCompositionClip(
  value: unknown,
  index = 0,
): CompositionClip {
  return decodeClip(value, index);
}

function decodeOutput(value: unknown): Readonly<Record<string, unknown>> {
  const wire = object(value, outputProfileKeys, "output");
  if (wire.profile_id !== OUTPUT_PROFILE_ID)
    fail("unsupported_output_profile", "output profile is unsupported");
  const frameRate = rational(wire.frame_rate, "output.frame_rate");
  const timeBase = rational(wire.time_base, "output.time_base");
  const pixelAspect = rational(wire.pixel_aspect, "output.pixel_aspect");
  const width = exactInteger(wire.width, 16, 1_920, "output.width");
  const height = exactInteger(wire.height, 16, 1_080, "output.height");
  if (
    frameRate.num !== 24 ||
    frameRate.den !== 1 ||
    timeBase.num !== 1 ||
    timeBase.den !== 24 ||
    pixelAspect.num !== 1 ||
    pixelAspect.den !== 1 ||
    width % 2 !== 0 ||
    height % 2 !== 0 ||
    width * height > 2_073_600
  )
    fail(
      "unsupported_output_profile",
      "output rational or dimension profile drifted",
    );
  const exact: Record<string, unknown> = {
    container: "mp4",
    video_codec: "h264",
    pixel_format: "yuv420p",
    color_policy: "bt709_sdr_limited_v1",
    audio_policy: "primary_embedded_follow_video_v1",
    audio_codec: "aac",
    sample_rate: 48_000,
    channels: 1,
    preview_max_av_drift_samples: 2_000,
    final_impulse_tolerance_samples: 2_048,
  };
  if (Object.entries(exact).some(([key, expected]) => wire[key] !== expected))
    fail("unsupported_output_profile", "output metadata drifted");
  return deepFreeze({
    profileId: OUTPUT_PROFILE_ID,
    frameRate,
    timeBase,
    width,
    height,
    durationFrames: exactInteger(
      wire.duration_frames,
      1,
      1_000_000,
      "output.duration_frames",
    ),
    container: "mp4",
    videoCodec: "h264",
    pixelFormat: "yuv420p",
    pixelAspect,
    colorPolicy: "bt709_sdr_limited_v1",
    audioPolicy: "primary_embedded_follow_video_v1",
    audioCodec: "aac",
    sampleRate: 48_000,
    channels: 1,
    previewMaxAvDriftSamples: 2_000,
    finalImpulseToleranceSamples: 2_048,
  });
}

function decodeCapability(value: unknown): Readonly<Record<string, unknown>> {
  const wire = object(value, capabilityProfileKeys, "capability");
  const expected: Record<string, unknown> = {
    engine_profile_id: ENGINE_PROFILE_ID,
    input_containers: ["mp4"],
    input_video_codecs: ["h264"],
    input_audio_codecs: ["aac"],
    input_pixel_formats: ["yuv420p"],
    media_transport: "intrinsic_html_media_element_v1",
    frame_observer: "request_video_frame_callback_v1",
    frame_event_fallbacks: ["seeked", "timeupdate"],
    visual_compositor: "canvas2d_ladder_v2",
    embedded_audio_policy: "primary_embedded_follow_video_v1",
    cross_origin_isolation_required: false,
    mse_required: false,
    webcodecs_required: false,
    network_policy: "same_origin_bounded_body_only_v1",
    active_video_limit: 2,
    warm_video_limit: 1,
    canvas_limit: 1,
    pending_rvfc_limit: 2,
    cancel_deadline_ms: 250,
    teardown_deadline_ms: 500,
    decision_receipt_fingerprints: [
      "sha256:3cd21f23932be62a4dfca721ce4cf2f93d78df7ce2210412c5155c159ddea797",
      "sha256:635892d0b5a513ec19980bf573493d1ba558c6d007cc8b8ac4884ef59c0e8e4b",
    ],
    observation_corpus_ids: [
      "cfr",
      "invalid",
      "lane",
      "mse",
      "truncated",
      "vfr",
    ],
    observation_corpus_fingerprints: [
      "sha256:1b77cf5e99d63696f613e9be79ef29c0d99fd8914da111b631bfdb69958cb603",
      "sha256:f7941036b79edad65b72beff2b81e9160624c1c759da2c8e875645992e504aae",
      "sha256:159d4c24e722ca8d085c2212fd9c9ffe92422b7413443dce20923a0bf3cb7c9c",
      "sha256:80173360d0925747b3ccdebce453eb2c13f8a748ae619214e94462d541d13fb9",
      "sha256:b24953b14f1255a49917b20f1680d2adfe2c056e99859396df4ab691c6a2f754",
      "sha256:e17aae228364b5d73d8eee1e5b0b068de176c6031d1c17f1b4ba5ff392985e16",
    ],
    probe_root_keys: ["format", "programs", "stream_groups", "streams"],
    probe_format_keys: ["duration", "format_name"],
    probe_video_stream_keys: [
      "avg_frame_rate",
      "codec_name",
      "codec_type",
      "height",
      "pix_fmt",
      "width",
    ],
    probe_audio_stream_keys: [
      "avg_frame_rate",
      "channel_layout",
      "channels",
      "codec_name",
      "codec_type",
      "sample_rate",
    ],
    probe_empty_array_keys: ["programs", "stream_groups"],
    audio_stream_rate_sentinel: "0/0",
  };
  if (
    capabilityProfileKeys.some(
      (key) => JSON.stringify(wire[key]) !== JSON.stringify(expected[key]),
    )
  )
    fail("unsupported_profile", "capability profile drifted");
  return deepFreeze({
    engineProfileId: ENGINE_PROFILE_ID,
    inputContainers: ["mp4"],
    inputVideoCodecs: ["h264"],
    inputAudioCodecs: ["aac"],
    inputPixelFormats: ["yuv420p"],
    mediaTransport: "intrinsic_html_media_element_v1",
    frameObserver: "request_video_frame_callback_v1",
    frameEventFallbacks: ["seeked", "timeupdate"],
    visualCompositor: "canvas2d_ladder_v2",
    embeddedAudioPolicy: "primary_embedded_follow_video_v1",
    crossOriginIsolationRequired: false,
    mseRequired: false,
    webcodecsRequired: false,
    networkPolicy: "same_origin_bounded_body_only_v1",
    activeVideoLimit: 2,
    warmVideoLimit: 1,
    canvasLimit: 1,
    pendingRvfcLimit: 2,
    cancelDeadlineMs: 250,
    teardownDeadlineMs: 500,
    decisionReceiptFingerprints: expected.decision_receipt_fingerprints,
    observationCorpusIds: expected.observation_corpus_ids,
    observationCorpusFingerprints: expected.observation_corpus_fingerprints,
    probeRootKeys: expected.probe_root_keys,
    probeFormatKeys: expected.probe_format_keys,
    probeVideoStreamKeys: expected.probe_video_stream_keys,
    probeAudioStreamKeys: expected.probe_audio_stream_keys,
    probeEmptyArrayKeys: expected.probe_empty_array_keys,
    audioStreamRateSentinel: "0/0",
  });
}

function decodeAudioExtension(
  value: unknown,
): Readonly<Record<string, unknown>> {
  const wire = object(value, audioExtensionKeys, "audio_extension");
  const expected: Record<string, unknown> = {
    schema: AUDIO_EXTENSION_SCHEMA,
    track_profile: "none_v1",
    command_namespace: INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
    command_members: [],
    preview_edit_capability: "unsupported",
    final_render_edit_capability: "unsupported",
    embedded_renderer_variant: "EmbeddedAudioSpanV1",
    independent_audio_renderer_variant: "none_v1",
    reason: "audio_editing_deferred",
  };
  if (
    audioExtensionKeys.some(
      (key) => JSON.stringify(wire[key]) !== JSON.stringify(expected[key]),
    )
  )
    fail("audio_editing_deferred", "independent audio extension is closed");
  return deepFreeze({
    schema: AUDIO_EXTENSION_SCHEMA,
    trackProfile: "none_v1",
    commandNamespace: INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
    commandMembers: [],
    previewEditCapability: "unsupported",
    finalRenderEditCapability: "unsupported",
    embeddedRendererVariant: "EmbeddedAudioSpanV1",
    independentAudioRendererVariant: "none_v1",
    reason: "audio_editing_deferred",
  });
}

/** Decode the closed audio extension shared by authoring and render contracts. */
export function decodeCompositionAudioExtension(
  value: unknown,
): Readonly<Record<string, unknown>> {
  return decodeAudioExtension(value);
}

/** Decode one closed blocker with the same vocabulary used by public snapshots. */
export function decodeCompositionBlocker(
  value: unknown,
  index = 0,
): Readonly<{ code: string; subjectId: string | null }> {
  const decoded = object(value, compositionBlockerKeys, `blockers[${index}]`);
  return Object.freeze({
    code: oneOf(decoded.code, BLOCKER_CODES, `blockers[${index}].code`),
    subjectId:
      decoded.subject_id === null
        ? null
        : exactIdentifier(decoded.subject_id, `blockers[${index}].subject_id`),
  });
}

function decodeRenderVocabulary(
  value: unknown,
): Readonly<Record<string, unknown>> {
  const wire = object(value, renderVocabularyKeys, "render_vocabulary");
  const expected: Record<string, unknown> = {
    schema: "h3.context.authoring_render_vocabulary.v1",
    request_schema: "h3.context.authoring_render_request.v1",
    job_schema: "h3.context.authoring_render_job.v1",
    receipt_schema: "h3.context.authoring_render_receipt.v1",
    states: RENDER_JOB_STATES,
    terminal_states: RENDER_TERMINAL_STATES,
    reasons: RENDER_TERMINAL_REASONS,
  };
  if (
    renderVocabularyKeys.some(
      (key) => JSON.stringify(wire[key]) !== JSON.stringify(expected[key]),
    )
  )
    fail("unsupported_profile", "render vocabulary drifted");
  return deepFreeze({
    schema: expected.schema,
    requestSchema: expected.request_schema,
    jobSchema: expected.job_schema,
    receiptSchema: expected.receipt_schema,
    states: [...RENDER_JOB_STATES],
    terminalStates: [...RENDER_TERMINAL_STATES],
    reasons: [...RENDER_TERMINAL_REASONS],
  });
}

function normalizeCanonical(
  value: unknown,
  depth = 0,
  maximumArrayItems = 256,
): unknown {
  if (depth > 32)
    fail("resource_limit", "canonical value exceeds maximum depth");
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value))
      fail(
        "invalid_contract",
        "canonical numbers must be interoperable integers",
      );
    return value;
  }
  if (typeof value === "string") {
    const normalized = value.normalize("NFC");
    if (normalized.length > 65_536)
      fail("resource_limit", "canonical string exceeds its bound");
    for (const character of normalized) {
      const point = character.codePointAt(0) ?? 0;
      if (point === 0 || (point >= 0xd800 && point <= 0xdfff))
        fail(
          "invalid_contract",
          "canonical string contains an unsafe code point",
        );
    }
    return normalized;
  }
  if (Array.isArray(value)) {
    if (value.length > maximumArrayItems)
      fail("resource_limit", "canonical array exceeds its bound");
    return value.map((item) =>
      normalizeCanonical(item, depth + 1, maximumArrayItems),
    );
  }
  if (typeof value === "object") {
    const wire = value as Record<string, unknown>;
    const keys = Object.keys(wire);
    if (keys.length > 256)
      fail("resource_limit", "canonical object exceeds its bound");
    const result: Record<string, unknown> = {};
    for (const key of [...keys].sort()) {
      if (!canonicalKey.test(key))
        fail("invalid_contract", "canonical object key is invalid");
      result[key] = normalizeCanonical(wire[key], depth + 1, maximumArrayItems);
    }
    return result;
  }
  fail("invalid_contract", "canonical value type is unsupported");
}

/** Fingerprint bounded composition material using the cross-language canonical form. */
export function compositionContractFingerprint(value: unknown): string {
  return sha256Text(JSON.stringify(normalizeCanonical(value, 0, 2_048)));
}

/**
 * Whether one authoring operation is admitted by the shipped profile.
 *
 * CRITICAL: this mirrors `composition_contract.operation_disposition` rule for rule, including the
 * order of the two refusals -- the audio namespace is checked before profile membership, so an
 * audio command never reports `operation_not_in_profile`. The M23-46 differential corpus compares
 * this function against its Python counterpart, so a divergence here is a cross-language contract
 * break rather than a local style choice. Do not reimplement it in a test: a copy written for the
 * corpus would agree with itself and prove nothing.
 */
export function operationDisposition(operationId: unknown): string {
  if (typeof operationId !== "string")
    fail("operation_not_in_profile", "operation ID is invalid");
  if (operationId.startsWith(`${INDEPENDENT_AUDIO_COMMAND_NAMESPACE}.`))
    fail("audio_editing_deferred", "independent audio editing is deferred");
  if (!(NLE_OPERATION_IDS as readonly string[]).includes(operationId))
    fail("operation_not_in_profile", "operation ID is outside the profile");
  return "accepted";
}

export function publicCompositionFingerprint(
  value: Record<string, unknown>,
): string {
  // CRITICAL: exclude only the self-referential fingerprint.  Omitting another field would allow
  // browser and backend to render different edits under one CAS identity.
  const material = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== "public_fingerprint"),
  );
  // CRITICAL: widen only this already-bounded public snapshot domain. Resolved scenes and every
  // unrelated canonical consumer retain the general 256-item collection ceiling.
  const text = JSON.stringify(normalizeCanonical(material, 0, 2_048));
  if (new TextEncoder().encode(text).length > 1_000_000)
    fail("resource_limit", "canonical snapshot exceeds its byte budget");
  return sha256Text(text);
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value as Record<string, unknown>))
      deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

export function decodePublicCompositionSnapshot(
  value: unknown,
): PublicCompositionSnapshot {
  const wire = object(value, publicCompositionSnapshotKeys, "snapshot");
  if (
    wire.schema !== PUBLIC_SNAPSHOT_SCHEMA ||
    wire.profile_id !== ENGINE_PROFILE_ID
  )
    fail("unsupported_profile", "public composition profile is unsupported");
  if (wire.operation_profile_id !== OPERATION_PROFILE_ID)
    fail("unsupported_profile", "operation profile is unsupported");
  const output = decodeOutput(wire.output);
  const capability = decodeCapability(wire.capability);
  const assets = Object.freeze(
    resourceArray(wire.assets, 128, "assets").map(decodeAsset),
  );
  if (assets.reduce((sum, asset) => sum + asset.landmarks.length, 0) > 2_048)
    fail("resource_limit", "snapshot landmark total exceeds the profile");
  const tracks = Object.freeze(
    resourceArray(wire.tracks, 8, "tracks").map(decodeTrack),
  );
  const clips = Object.freeze(
    resourceArray(wire.clips, 128, "clips").map(decodeClip),
  );
  const blockers = Object.freeze(
    resourceArray(wire.blockers, 128, "blockers").map((row, index) => {
      const decoded = object(row, compositionBlockerKeys, `blockers[${index}]`);
      return Object.freeze({
        code: oneOf(decoded.code, BLOCKER_CODES, `blockers[${index}].code`),
        subjectId:
          decoded.subject_id === null
            ? null
            : exactIdentifier(
                decoded.subject_id,
                `blockers[${index}].subject_id`,
              ),
      });
    }),
  );
  const assetById = new Map(assets.map((asset) => [asset.assetId, asset]));
  const trackById = new Map(tracks.map((track) => [track.trackId, track]));
  if (
    assetById.size !== assets.length ||
    trackById.size !== tracks.length ||
    new Set(clips.map((clip) => clip.clipId)).size !== clips.length ||
    new Set(tracks.map((track) => track.order)).size !== tracks.length
  )
    fail(
      "invalid_contract",
      "asset, track, clip IDs and track order must be unique",
    );
  if (tracks.filter((track) => track.kind === "primary_video").length !== 1)
    fail("invalid_contract", "exactly one primary video track is required");
  const duration = output.durationFrames as number;
  for (const clip of clips) {
    const track = trackById.get(clip.trackId);
    if (!track) fail("invalid_contract", "clip references an unknown track");
    if (clip.startFrame + clip.durationFrames > duration)
      fail("source_range_unavailable", "clip exceeds output duration");
    let boundAudio = false;
    if (track.kind === "text_overlay") {
      if (
        clip.assetId !== null ||
        clip.text === null ||
        clip.sourceStartFrame !== 0
      )
        fail("invalid_contract", "text clip shape is invalid");
      const font = assetById.get(clip.text.fontAssetId as string);
      if (!font || font.kind !== "font")
        fail("invalid_contract", "text clip font is not admitted");
    } else {
      if (clip.assetId === null || clip.text !== null)
        fail("invalid_contract", "media clip shape is invalid");
      const asset = assetById.get(clip.assetId);
      const requiredKind =
        track.kind === "primary_video" || track.kind === "video_overlay"
          ? "video"
          : "image";
      if (!asset || asset.kind !== requiredKind)
        fail("invalid_contract", "clip asset kind does not match its track");
      // CRITICAL: muting is track-derived policy, never a source-fact rewrite. Requiring a
      // policy label rejects real video and contradicts primary/overlay reuse of one asset.
      // Legacy labels remain readable; backend render admission requires measured facts.
      if (
        asset.kind === "video" &&
        clip.sourceStartFrame >= (asset.sourceFrameCount ?? 0)
      )
        fail("source_range_unavailable", "clip exceeds admitted source range");
      if (asset.kind !== "video" && clip.sourceStartFrame !== 0)
        fail(
          "source_range_unavailable",
          "untimed media cannot carry a source offset",
        );
      // Only a video asset can declare bound audio (`decodeAsset`).
      boundAudio = asset.embeddedAudio === "present_bound";
    }
    // A clip's audio member scales its own source's embedded audio, so it is admitted only
    // where there is bound audio to scale. Track placement does not decide it.
    if (!isIdentityClipAudio(clip.audio) && !boundAudio)
      fail("invalid_contract", "clip audio adjustments need bound audio");
    if (clip.transition.kind === "cross_dissolve_v1") {
      const hasLowerParticipant = clips.some((other) => {
        const otherTrack = trackById.get(other.trackId);
        return (
          other.clipId !== clip.clipId &&
          other.enabled &&
          otherTrack?.enabled === true &&
          otherTrack.order < track.order &&
          other.startFrame <= clip.startFrame &&
          other.startFrame + other.durationFrames >=
            clip.startFrame + clip.transition.durationFrames
        );
      });
      if (!hasLowerParticipant)
        fail(
          "invalid_contract",
          "cross dissolve requires a lower layer for its full duration",
        );
    }
  }
  const videoClips = clips.filter((clip) => {
    const track = trackById.get(clip.trackId);
    return (
      clip.enabled &&
      track?.enabled === true &&
      (track.kind === "primary_video" || track.kind === "video_overlay")
    );
  });
  if (
    videoClips.some(
      (clip) =>
        videoClips.filter(
          (candidate) =>
            candidate.startFrame <= clip.startFrame &&
            clip.startFrame < candidate.startFrame + candidate.durationFrames,
        ).length > 2,
    )
  )
    fail("resource_limit", "active video layers exceed the engine profile");
  const publicFingerprint = exactFingerprint(
    wire.public_fingerprint,
    "snapshot.public_fingerprint",
  );
  if (publicFingerprint !== publicCompositionFingerprint(wire))
    fail("stale_snapshot", "public fingerprint does not match the snapshot");
  return deepFreeze({
    schema: PUBLIC_SNAPSHOT_SCHEMA,
    profileId: ENGINE_PROFILE_ID,
    operationProfileId: OPERATION_PROFILE_ID,
    projectId: exactIdentifier(wire.project_id, "snapshot.project_id"),
    workspaceHandle: exactIdentifier(
      wire.workspace_handle,
      "snapshot.workspace_handle",
    ),
    workspaceRevision: exactInteger(
      wire.workspace_revision,
      0,
      1_000_000,
      "snapshot.workspace_revision",
    ),
    workspaceFingerprint: exactFingerprint(
      wire.workspace_fingerprint,
      "snapshot.workspace_fingerprint",
    ),
    timelineRevision: exactInteger(
      wire.timeline_revision,
      0,
      1_000_000,
      "snapshot.timeline_revision",
    ),
    timelineFingerprint: exactFingerprint(
      wire.timeline_fingerprint,
      "snapshot.timeline_fingerprint",
    ),
    publicFingerprint,
    output,
    capability,
    assets,
    tracks,
    clips,
    audioExtension: decodeAudioExtension(wire.audio_extension),
    blockers,
    renderVocabulary: decodeRenderVocabulary(wire.render_vocabulary),
  });
}

class JsonScanner {
  private cursor = 0;
  constructor(private readonly source: string) {}

  parse(): void {
    this.value();
    this.space();
    if (this.cursor !== this.source.length)
      fail("invalid_contract", "snapshot JSON has trailing content");
  }

  private space(): void {
    while (/\s/u.test(this.source[this.cursor] ?? "")) this.cursor += 1;
  }

  private value(): void {
    this.space();
    const token = this.source[this.cursor];
    if (token === "{") return this.object();
    if (token === "[") return this.array();
    if (token === '"') {
      this.string();
      return;
    }
    const rest = this.source.slice(this.cursor);
    const match =
      /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/u.exec(
        rest,
      );
    if (!match) fail("invalid_contract", "snapshot JSON token is invalid");
    this.cursor += match[0].length;
  }

  private string(): string {
    const start = this.cursor;
    this.cursor += 1;
    while (this.cursor < this.source.length) {
      const token = this.source[this.cursor];
      if (token === '"') {
        this.cursor += 1;
        try {
          return JSON.parse(this.source.slice(start, this.cursor)) as string;
        } catch {
          fail("invalid_contract", "snapshot JSON string is invalid");
        }
      }
      if (token === "\\") this.cursor += 2;
      else this.cursor += 1;
    }
    fail("invalid_contract", "snapshot JSON string is unterminated");
  }

  private object(): void {
    this.cursor += 1;
    this.space();
    const keys = new Set<string>();
    if (this.source[this.cursor] === "}") {
      this.cursor += 1;
      return;
    }
    for (;;) {
      this.space();
      if (this.source[this.cursor] !== '"')
        fail("invalid_contract", "snapshot JSON object key is invalid");
      const key = this.string();
      if (keys.has(key))
        fail("invalid_contract", "snapshot JSON contains a duplicate member");
      keys.add(key);
      this.space();
      if (this.source[this.cursor] !== ":")
        fail("invalid_contract", "snapshot JSON object separator is invalid");
      this.cursor += 1;
      this.value();
      this.space();
      const token = this.source[this.cursor];
      if (token === "}") {
        this.cursor += 1;
        return;
      }
      if (token !== ",")
        fail("invalid_contract", "snapshot JSON object delimiter is invalid");
      this.cursor += 1;
    }
  }

  private array(): void {
    this.cursor += 1;
    this.space();
    if (this.source[this.cursor] === "]") {
      this.cursor += 1;
      return;
    }
    for (;;) {
      this.value();
      this.space();
      const token = this.source[this.cursor];
      if (token === "]") {
        this.cursor += 1;
        return;
      }
      if (token !== ",")
        fail("invalid_contract", "snapshot JSON array delimiter is invalid");
      this.cursor += 1;
    }
  }
}

export function decodeCompositionJson(
  value: string,
): PublicCompositionSnapshot {
  if (typeof value !== "string")
    fail("invalid_contract", "snapshot JSON must be text");
  // SECURITY: JSON.parse discards earlier duplicate members.  Scan first or an attacker can make
  // validation and fingerprint review appear to describe a different member than the one consumed.
  new JsonScanner(value).parse();
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    fail("invalid_contract", "snapshot JSON is invalid");
  }
  return decodePublicCompositionSnapshot(parsed);
}

const resolvedOperations = [
  "SelectSourceRangeV1",
  "CropV1",
  "Transform2DV1",
  "ColorAdjustV1",
  "OpacityV1",
  "BlendV1",
  "CrossDissolveV1",
  "DrawTextV1",
] as const;

export function decodeResolvedScene(value: unknown): ResolvedCompositionScene {
  const wire = object(value, resolvedSceneKeys, "resolved_scene");
  if (
    wire.schema !== RESOLVED_SCENE_SCHEMA ||
    wire.profile_id !== ENGINE_PROFILE_ID
  )
    fail("unsupported_profile", "resolved scene profile is unsupported");
  const layers = Object.freeze(
    resourceArray(wire.layers, 128, "resolved_scene.layers").map(
      (row, index) => {
        const name = `resolved_scene.layers[${index}]`;
        const layer = object(row, resolvedLayerKeys, name);
        const sourceFrame =
          layer.source_frame === null
            ? null
            : exactInteger(
                layer.source_frame,
                0,
                1_000_000,
                `${name}.source_frame`,
              );
        const sourcePts =
          layer.source_pts === null
            ? null
            : exactInteger(layer.source_pts, 0, MAX_SAFE, `${name}.source_pts`);
        if ((sourceFrame === null) !== (sourcePts === null))
          fail(
            "invalid_timing",
            `${name} source timing must be jointly available`,
          );
        const operationIds = Object.freeze(
          resourceArray(
            layer.operation_ids,
            resolvedOperations.length,
            `${name}.operation_ids`,
          ).map((member, memberIndex) =>
            oneOf(
              member,
              resolvedOperations,
              `${name}.operation_ids[${memberIndex}]`,
            ),
          ),
        );
        if (new Set(operationIds).size !== operationIds.length)
          fail("invalid_contract", `${name} operation IDs must be unique`);
        const effectWire = object(layer.effect, effectKeys, `${name}.effect`);
        const transitionElapsedFrames =
          layer.transition_elapsed_frames === null
            ? null
            : exactInteger(
                layer.transition_elapsed_frames,
                0,
                299,
                `${name}.transition_elapsed_frames`,
              );
        const transform = numericObject(
          layer.transform,
          transform2DKeys,
          {
            anchor_x_bp: [0, 10_000],
            anchor_y_bp: [0, 10_000],
            position_x_bp: [-40_000, 40_000],
            position_y_bp: [-40_000, 40_000],
            scale_x_bp: [1, 80_000],
            scale_y_bp: [1, 80_000],
            rotation_mdeg: [-180_000, 180_000],
          },
          `${name}.transform`,
        );
        const crop = numericObject(
          layer.crop,
          cropKeys,
          {
            left_bp: [0, 9_999],
            top_bp: [0, 9_999],
            right_bp: [0, 9_999],
            bottom_bp: [0, 9_999],
          },
          `${name}.crop`,
        );
        if (
          (crop.left_bp ?? 0) + (crop.right_bp ?? 0) >= 10_000 ||
          (crop.top_bp ?? 0) + (crop.bottom_bp ?? 0) >= 10_000
        )
          fail("invalid_contract", `${name}.crop removes the full image`);
        const text = decodeText(layer.text, `${name}.text`);
        const effectKind = oneOf(
          effectWire.kind,
          ["none", "color_adjust_v1"] as const,
          `${name}.effect.kind`,
        );
        const effect = Object.freeze({
          kind: effectKind,
          brightnessPermille: exactInteger(
            effectWire.brightness_permille,
            -1_000,
            1_000,
            `${name}.effect.brightness`,
          ),
          contrastPermille: exactInteger(
            effectWire.contrast_permille,
            0,
            2_000,
            `${name}.effect.contrast`,
          ),
          saturationPermille: exactInteger(
            effectWire.saturation_permille,
            0,
            2_000,
            `${name}.effect.saturation`,
          ),
        });
        if (
          effect.kind === "none" &&
          (effect.brightnessPermille !== 0 ||
            effect.contrastPermille !== 1_000 ||
            effect.saturationPermille !== 1_000)
        )
          fail(
            "invalid_contract",
            `${name}.effect none must use identity values`,
          );
        const expectedOperations: string[] = [
          "SelectSourceRangeV1",
          "CropV1",
          "Transform2DV1",
        ];
        if (effect.kind !== "none") expectedOperations.push("ColorAdjustV1");
        expectedOperations.push("OpacityV1", "BlendV1");
        if (transitionElapsedFrames !== null)
          expectedOperations.push("CrossDissolveV1");
        if (text !== null) expectedOperations.push("DrawTextV1");
        // IMPORTANT: operation order is executable resolver semantics. Accepting a subset or a
        // permutation lets a forged browser hand-off render a different scene under one identity.
        if (JSON.stringify(operationIds) !== JSON.stringify(expectedOperations))
          fail(
            "invalid_contract",
            `${name} operations do not match the resolved values`,
          );
        return deepFreeze({
          clipId: exactIdentifier(layer.clip_id, `${name}.clip_id`),
          assetId:
            layer.asset_id === null
              ? null
              : exactIdentifier(layer.asset_id, `${name}.asset_id`),
          trackId: exactIdentifier(layer.track_id, `${name}.track_id`),
          sourceFrame,
          sourcePts,
          transitionElapsedFrames,
          operationIds,
          transform,
          crop,
          opacityBp: exactInteger(
            layer.opacity_bp,
            0,
            10_000,
            `${name}.opacity_bp`,
          ),
          blend: oneOf(
            layer.blend,
            ["normal", "multiply", "screen"] as const,
            `${name}.blend`,
          ),
          text,
          effect,
        });
      },
    ),
  );
  let audioSpan: Readonly<Record<string, number | string>> | null = null;
  if (wire.audio_span !== null) {
    const audio = object(
      wire.audio_span,
      embeddedAudioSpanKeys,
      "resolved_scene.audio_span",
    );
    const outputStart = exactInteger(
      audio.output_start_sample,
      0,
      MAX_SAFE,
      "audio output start",
    );
    const outputEnd = exactInteger(
      audio.output_end_sample,
      1,
      MAX_SAFE,
      "audio output end",
    );
    const sourceStart = exactInteger(
      audio.source_start_sample,
      0,
      MAX_SAFE,
      "audio source start",
    );
    const sourceEnd = exactInteger(
      audio.source_end_sample,
      1,
      MAX_SAFE,
      "audio source end",
    );
    if (outputEnd <= outputStart || sourceEnd <= sourceStart)
      fail("invalid_timing", "resolved audio span must be positive");
    audioSpan = Object.freeze({
      assetId: exactIdentifier(audio.asset_id, "audio asset_id"),
      clipId: exactIdentifier(audio.clip_id, "audio clip_id"),
      outputStartSample: outputStart,
      outputEndSample: outputEnd,
      sourceStartSample: sourceStart,
      sourceEndSample: sourceEnd,
    });
  }
  const blockers = Object.freeze(
    resourceArray(wire.blockers, 128, "resolved_scene.blockers").map(
      (row, index) => {
        const blocker = object(
          row,
          compositionBlockerKeys,
          `resolved blocker ${index}`,
        );
        return Object.freeze({
          code: oneOf(
            blocker.code,
            BLOCKER_CODES,
            `resolved blocker ${index}.code`,
          ),
          subjectId:
            blocker.subject_id === null
              ? null
              : exactIdentifier(
                  blocker.subject_id,
                  `resolved blocker ${index}.subject_id`,
                ),
        });
      },
    ),
  );
  const canonical = JSON.stringify(normalizeCanonical(wire));
  if (new TextEncoder().encode(canonical).length > 131_072)
    fail("resource_limit", "resolved scene exceeds its byte budget");
  return deepFreeze({
    schema: RESOLVED_SCENE_SCHEMA,
    profileId: ENGINE_PROFILE_ID,
    publicFingerprint: exactFingerprint(
      wire.public_fingerprint,
      "resolved public fingerprint",
    ),
    frame: exactInteger(wire.frame, 0, 999_999, "resolved frame"),
    layers,
    audioSpan,
    blockers,
  });
}
