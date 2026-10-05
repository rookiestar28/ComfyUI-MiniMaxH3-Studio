// M25-16: the browser composition-monitor scene resolver.
//
// This is a field-for-field port of the pure backend `resolve_composition` in
// `comfyui_h3_context/core/composition_contract.py`. The monitor needs one resolved scene per
// presented output frame, and the accepted contract forbids per-frame HTTP, so the resolution
// runs locally over the accepted public snapshot. It is an approximation authority for the
// browser preview only: the backend renderer and its receipts remain the final-output truth.
//
// CRITICAL: source timing is exact rational arithmetic (BigInt), never float. Flooring or
// rounding an intermediate lets output-frame deltas invent source frames that the admitted
// landmarks do not contain, which is exactly the class of drift the backend rejects.

import {
  CompositionContractError,
  ENGINE_PROFILE_ID,
  RESOLVED_SCENE_SCHEMA,
  type CompositionClip,
  type PublicCompositionAsset,
  type PublicCompositionSnapshot,
} from "../contracts/compositionCodec";

const MAX_RESOLVED_LAYERS = 128;
const MAX_RESOLVER_WORK_UNITS = 4_096;
const OUTPUT_SAMPLE_RATE = 48_000n;

type Fraction = Readonly<{ num: bigint; den: bigint }>;
type Rational = Readonly<{ num: number; den: number }>;

function fraction(num: bigint, den: bigint): Fraction {
  if (den === 0n)
    throw new CompositionContractError("invalid_timing", "zero denominator");
  if (den < 0n) return { num: -num, den: -den };
  return { num, den };
}

function add(left: Fraction, right: Fraction): Fraction {
  return fraction(
    left.num * right.den + right.num * left.den,
    left.den * right.den,
  );
}

function compare(left: Fraction, right: Fraction): -1 | 0 | 1 {
  const difference = left.num * right.den - right.num * left.den;
  return difference < 0n ? -1 : difference > 0n ? 1 : 0;
}

function floorDivide(num: bigint, den: bigint): bigint {
  // Python `//` floors toward negative infinity; BigInt `/` truncates toward zero.
  const quotient = num / den;
  const remainder = num % den;
  return remainder !== 0n && remainder < 0n !== den < 0n
    ? quotient - 1n
    : quotient;
}

function outputFrameRate(snapshot: PublicCompositionSnapshot): Rational {
  const value = snapshot.output.frameRate as Rational | undefined;
  if (
    value === undefined ||
    !Number.isSafeInteger(value.num) ||
    !Number.isSafeInteger(value.den) ||
    value.num < 1 ||
    value.den < 1
  )
    throw new CompositionContractError(
      "invalid_timing",
      "output frame rate must be positive",
    );
  return value;
}

function outputDurationFrames(snapshot: PublicCompositionSnapshot): number {
  const value = snapshot.output.durationFrames;
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1)
    throw new CompositionContractError(
      "invalid_contract",
      "output duration is unavailable",
    );
  return value;
}

/**
 * The exact source tick reached after `elapsedFrames` output frames from the admitted landmark
 * `sourceStartFrame`, or `null` when that landmark is not admitted. Mirrors
 * `_source_target_tick_exact`, including the declared-source-end refusal.
 */
export function sourceTargetTickExact(
  asset: PublicCompositionAsset,
  sourceStartFrame: number,
  elapsedFrames: number,
  frameRate: Rational,
  allowSourceEnd = false,
): Fraction | null {
  if (asset.sourceTimeBase === null)
    throw new CompositionContractError(
      "invalid_timing",
      "video source timebase is missing",
    );
  const base = asset.landmarks.find(
    (landmark) => landmark.frameIndex === sourceStartFrame,
  );
  if (base === undefined) return null;
  const timeBase = asset.sourceTimeBase;
  const targetTick = add(
    fraction(BigInt(base.pts), 1n),
    fraction(
      BigInt(elapsedFrames) * BigInt(frameRate.den) * BigInt(timeBase.den),
      BigInt(frameRate.num) * BigInt(timeBase.num),
    ),
  );
  const last = asset.landmarks[asset.landmarks.length - 1];
  if (last === undefined) return null;
  const sourceEnd = fraction(BigInt(last.pts + last.durationTicks), 1n);
  const order = compare(targetTick, sourceEnd);
  if (order > 0 || (order === 0 && !allowSourceEnd))
    throw new CompositionContractError(
      "source_range_unavailable",
      "source target exceeds declared timing",
    );
  return targetTick;
}

function sourceTargetTick(
  asset: PublicCompositionAsset,
  sourceStartFrame: number,
  elapsedFrames: number,
  frameRate: Rational,
  allowSourceEnd = false,
): bigint | null {
  const exact = sourceTargetTickExact(
    asset,
    sourceStartFrame,
    elapsedFrames,
    frameRate,
    allowSourceEnd,
  );
  if (exact === null) return null;
  return floorDivide(exact.num, exact.den);
}

/**
 * The admitted source-frame identity at one exact output-time boundary, or a typed refusal
 * when the boundary is not exactly representable. Mirrors `resolve_source_landmark_after_elapsed`;
 * the trim gesture uses it only to label a draft as provisionally representable -- the backend
 * still decides admission.
 */
export function resolveSourceLandmarkAfterElapsed(
  asset: PublicCompositionAsset,
  sourceStartFrame: number,
  elapsedFrames: number,
  frameRate: Rational,
): number {
  if (asset.kind !== "video")
    throw new CompositionContractError(
      "invalid_contract",
      "source boundary requires an admitted video asset",
    );
  const targetTick = sourceTargetTickExact(
    asset,
    sourceStartFrame,
    elapsedFrames,
    frameRate,
  );
  if (targetTick !== null) {
    const landmark = asset.landmarks.find(
      (item) => compare(fraction(BigInt(item.pts), 1n), targetTick) === 0,
    );
    if (landmark !== undefined) return landmark.frameIndex;
  }
  throw new CompositionContractError(
    "source_range_unavailable",
    "source boundary lacks an exact admitted timing landmark",
  );
}

function sourceAtFrame(
  asset: PublicCompositionAsset,
  sourceStartFrame: number,
  elapsedFrames: number,
  frameRate: Rational,
): Readonly<{
  sourceFrame: number | null;
  sourcePts: number | null;
  targetTick: bigint | null;
}> {
  const targetTick = sourceTargetTick(
    asset,
    sourceStartFrame,
    elapsedFrames,
    frameRate,
  );
  if (targetTick === null)
    return { sourceFrame: null, sourcePts: null, targetTick: null };
  let selected: PublicCompositionAsset["landmarks"][number] | undefined;
  for (const landmark of asset.landmarks) {
    if (BigInt(landmark.pts) > targetTick) break;
    selected = landmark;
  }
  if (selected === undefined)
    return { sourceFrame: null, sourcePts: null, targetTick };
  return {
    sourceFrame: selected.frameIndex,
    sourcePts: selected.pts,
    targetTick,
  };
}

function textWire(
  text: CompositionClip["text"],
): Record<string, unknown> | null {
  if (text === null) return null;
  return {
    content: text.content,
    font_asset_id: text.fontAssetId,
    size_px: text.sizePx,
    weight: text.weight,
    style: text.style,
    align: text.align,
    line_height_bp: text.lineHeightBp,
    fill_rgba: [...(text.fillRgba as readonly number[])],
    background_rgba:
      text.backgroundRgba === null
        ? null
        : [...(text.backgroundRgba as readonly number[])],
  };
}

function effectWire(
  effect: CompositionClip["effect"],
): Record<string, unknown> {
  return {
    kind: effect.kind,
    brightness_permille: effect.brightnessPermille,
    contrast_permille: effect.contrastPermille,
    saturation_permille: effect.saturationPermille,
  };
}

export type ResolvedSceneWire = Readonly<{
  schema: typeof RESOLVED_SCENE_SCHEMA;
  profile_id: typeof ENGINE_PROFILE_ID;
  public_fingerprint: string;
  frame: number;
  layers: readonly Readonly<Record<string, unknown>>[];
  audio_span: Readonly<Record<string, number | string>> | null;
  blockers: readonly Readonly<{ code: string; subject_id: string | null }>[];
}>;

/**
 * Resolve one output frame of the accepted snapshot into the wire shape that
 * `decodeResolvedScene` admits. Throws `CompositionContractError` for an out-of-range frame or
 * an exhausted resolver budget, exactly like the backend.
 */
export function resolveCompositionScene(
  snapshot: PublicCompositionSnapshot,
  frame: number,
): ResolvedSceneWire {
  const durationFrames = outputDurationFrames(snapshot);
  if (
    typeof frame !== "number" ||
    !Number.isSafeInteger(frame) ||
    frame < 0 ||
    frame >= durationFrames
  )
    throw new CompositionContractError(
      "source_range_unavailable",
      "output frame is outside the composition",
    );
  const frameRate = outputFrameRate(snapshot);
  const trackById = new Map(
    snapshot.tracks.map((track) => [track.trackId, track] as const),
  );
  const assetById = new Map(
    snapshot.assets.map((asset) => [asset.assetId, asset] as const),
  );
  const track = (clip: CompositionClip) => {
    const value = trackById.get(clip.trackId);
    if (value === undefined)
      throw new CompositionContractError(
        "invalid_contract",
        "clip names an unknown track",
      );
    return value;
  };
  const asset = (assetId: string) => {
    const value = assetById.get(assetId);
    if (value === undefined)
      throw new CompositionContractError(
        "invalid_contract",
        "clip names an unknown asset",
      );
    return value;
  };
  const active = snapshot.clips.filter(
    (clip) =>
      clip.enabled &&
      track(clip).enabled &&
      clip.startFrame <= frame &&
      frame < clip.startFrame + clip.durationFrames,
  );
  const workUnits = snapshot.clips.length + active.length * 3;
  if (
    workUnits > MAX_RESOLVER_WORK_UNITS ||
    active.length > MAX_RESOLVED_LAYERS
  )
    throw new CompositionContractError(
      "resource_limit",
      "resolver work exceeds the closed budget",
    );
  active.sort((left, right) => {
    const order = track(left).order - track(right).order;
    if (order !== 0) return order;
    const start = left.startFrame - right.startFrame;
    if (start !== 0) return start;
    return left.clipId < right.clipId ? -1 : left.clipId > right.clipId ? 1 : 0;
  });
  const layers: Record<string, unknown>[] = [];
  const blockers: { code: string; subject_id: string | null }[] =
    snapshot.blockers.map((blocker) => ({
      code: blocker.code,
      subject_id: blocker.subjectId,
    }));
  const timingByClip = new Map<string, ReturnType<typeof sourceAtFrame>>();
  for (const clip of active) {
    const clipTrack = track(clip);
    const localFrame = frame - clip.startFrame;
    let timing: ReturnType<typeof sourceAtFrame> = {
      sourceFrame: null,
      sourcePts: null,
      targetTick: null,
    };
    if (clip.assetId !== null && asset(clip.assetId).kind === "video") {
      timing = sourceAtFrame(
        asset(clip.assetId),
        clip.sourceStartFrame,
        localFrame,
        frameRate,
      );
      if (timing.sourceFrame === null)
        blockers.push({ code: "timing_unavailable", subject_id: clip.clipId });
    }
    timingByClip.set(clip.clipId, timing);
    const operationIds = ["SelectSourceRangeV1", "CropV1", "Transform2DV1"];
    if (clip.effect.kind !== "none") operationIds.push("ColorAdjustV1");
    operationIds.push("OpacityV1", "BlendV1");
    let transitionElapsed: number | null = null;
    if (
      clip.transition.kind === "cross_dissolve_v1" &&
      localFrame < clip.transition.durationFrames
    ) {
      transitionElapsed = localFrame;
      operationIds.push("CrossDissolveV1");
    }
    if (clipTrack.kind === "text_overlay") operationIds.push("DrawTextV1");
    layers.push({
      clip_id: clip.clipId,
      asset_id: clip.assetId,
      track_id: clip.trackId,
      source_frame: timing.sourceFrame,
      source_pts: timing.sourcePts,
      transition_elapsed_frames: transitionElapsed,
      operation_ids: operationIds,
      transform: { ...clip.transform },
      crop: { ...clip.crop },
      opacity_bp: clip.opacityBp,
      blend: clip.blend,
      text: textWire(clip.text),
      effect: effectWire(clip.effect),
    });
  }

  // CRITICAL: only track role selects audio owners; an audible overlay (including the same
  // asset as a primary clip) must never add a second span or rewrite the source's audio facts.
  const primary = active.filter((clip) => track(clip).kind === "primary_video");
  let owner: CompositionClip | undefined;
  for (const clip of primary)
    if (
      owner === undefined ||
      clip.startFrame > owner.startFrame ||
      (clip.startFrame === owner.startFrame && clip.clipId > owner.clipId)
    )
      owner = clip;
  let audioSpan: Record<string, number | string> | null = null;
  if (owner !== undefined && owner.assetId !== null) {
    const ownerAsset = asset(owner.assetId);
    if (ownerAsset.embeddedAudio === "present_bound") {
      const timing = timingByClip.get(owner.clipId);
      if (
        timing === undefined ||
        timing.sourceFrame === null ||
        timing.targetTick === null ||
        ownerAsset.sourceSampleCount === null ||
        ownerAsset.sourceTimeBase === null
      )
        blockers.push({
          code: "embedded_audio_unavailable",
          subject_id: owner.clipId,
        });
      else {
        const outputStart = floorDivide(
          BigInt(frame) * OUTPUT_SAMPLE_RATE,
          24n,
        );
        const outputEnd = floorDivide(
          BigInt(frame + 1) * OUTPUT_SAMPLE_RATE,
          24n,
        );
        let sourceEndTick = sourceTargetTick(
          ownerAsset,
          owner.sourceStartFrame,
          frame - owner.startFrame + 1,
          frameRate,
          true,
        );
        if (sourceEndTick === null) {
          blockers.push({
            code: "embedded_audio_unavailable",
            subject_id: owner.clipId,
          });
          sourceEndTick = timing.targetTick;
        }
        const timeBase = ownerAsset.sourceTimeBase;
        const sourceStart = floorDivide(
          timing.targetTick * BigInt(timeBase.num) * OUTPUT_SAMPLE_RATE,
          BigInt(timeBase.den),
        );
        const sourceEnd = floorDivide(
          sourceEndTick * BigInt(timeBase.num) * OUTPUT_SAMPLE_RATE,
          BigInt(timeBase.den),
        );
        // IMPORTANT: adjacent output frames can quantize to one coarse source tick. Never
        // emit a zero-length span that the decoder correctly rejects as invalid timing.
        if (
          sourceEnd <= sourceStart ||
          sourceEnd > BigInt(ownerAsset.sourceSampleCount)
        )
          blockers.push({
            code: "embedded_audio_unavailable",
            subject_id: owner.clipId,
          });
        else
          audioSpan = {
            asset_id: ownerAsset.assetId,
            clip_id: owner.clipId,
            output_start_sample: Number(outputStart),
            output_end_sample: Number(outputEnd),
            source_start_sample: Number(sourceStart),
            source_end_sample: Number(sourceEnd),
          };
      }
    } else if (ownerAsset.embeddedAudio === "unavailable")
      blockers.push({
        code: "embedded_audio_unavailable",
        subject_id: owner.clipId,
      });
  }
  const unique: { code: string; subject_id: string | null }[] = [];
  const seen = new Set<string>();
  for (const blocker of blockers) {
    const key = `${blocker.code}\u0000${blocker.subject_id ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(blocker);
  }
  return Object.freeze({
    schema: RESOLVED_SCENE_SCHEMA,
    profile_id: ENGINE_PROFILE_ID,
    public_fingerprint: snapshot.publicFingerprint,
    frame,
    layers,
    audio_span: audioSpan,
    blockers: unique,
  });
}
