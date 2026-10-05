import {
  ENGINE_PROFILE_ID,
  type PublicCompositionSnapshot,
  type ResolvedCompositionScene,
} from "../contracts/compositionCodec";

export const VISUAL_COMPOSITOR_RECEIPT_SCHEMA =
  "h3.visual_compositor_receipt.v1" as const;

export type VisualLayerResource =
  | Readonly<{
      kind: "video" | "image";
      source: CanvasImageSource;
      nativeWidth: number;
      nativeHeight: number;
    }>
  | Readonly<{ kind: "font"; family: string }>;

export type VisualLayerResources = ReadonlyMap<string, VisualLayerResource>;

export type VisualCompositorBlocker =
  | "invalid_contract"
  | "stale_snapshot"
  | "resource_limit"
  | "source_unavailable"
  | "canvas_unavailable"
  | "closed";

export type VisualCompositorReceipt = Readonly<{
  schema: typeof VISUAL_COMPOSITOR_RECEIPT_SCHEMA;
  status: "presented" | "unavailable" | "closed";
  profileId: typeof ENGINE_PROFILE_ID;
  publicFingerprint: string;
  frame: number | null;
  generation: number;
  previewWidth: number;
  previewHeight: number;
  renderedLayerCount: number;
  blocker: VisualCompositorBlocker | null;
  browserPreviewOnly: true;
}>;

/** The measured picture area the preview is presented in, in CSS pixels plus the device ratio. */
export type PreviewBox = Readonly<{
  cssWidth: number;
  cssHeight: number;
  devicePixelRatio: number;
}>;

/**
 * The rung a composition renders on.
 *
 * `native` draws each layer straight onto the one canvas with the engine's own alpha, blend and
 * colour filter; `software` is the incumbent read-modify-write transaction. M25-45's measured
 * matrix chose between them: see the item's implementation record, decisions D1 to D4.
 */
export type PreviewPath = "native" | "software";

export type VisualCompositor = Readonly<{
  render(
    scene: ResolvedCompositionScene,
    resources: VisualLayerResources,
    generation: number,
  ): VisualCompositorReceipt;
  clear(generation?: number): VisualCompositorReceipt;
  close(): VisualCompositorReceipt;
  /** Re-set the backing store for a newly measured picture box. It paints no old pixels. */
  resize(box: PreviewBox): VisualCompositorReceipt;
  /** Update scene authority without resetting the held picture; false requires full replacement. */
  replace(snapshot: PublicCompositionSnapshot): boolean;
  path(): PreviewPath;
  /** Latest successfully presented output-space geometry for one visual layer. */
  layerGeometry(clipId: string): VisualLayerGeometry | null;
}>;

export type PreviewSize = Readonly<{
  width: number;
  height: number;
  scale: number;
}>;

type NumericRecord = Readonly<Record<string, number>>;
type DecodedLayer = Readonly<{
  clipId: string;
  assetId: string | null;
  trackId: string;
  sourceFrame: number | null;
  sourcePts: number | null;
  transitionElapsedFrames: number | null;
  operationIds: readonly string[];
  transform: NumericRecord;
  crop: NumericRecord;
  opacityBp: number;
  blend: "normal" | "multiply" | "screen";
  text: Readonly<Record<string, unknown>> | null;
  effect: Readonly<Record<string, number | string>>;
}>;

export type VisualLayerGeometry = Readonly<{
  nativeCrop: Readonly<{
    left: number;
    top: number;
    width: number;
    height: number;
  }>;
  sourceCrop: Readonly<{
    left: number;
    top: number;
    width: number;
    height: number;
  }>;
  scaledWidth: number;
  scaledHeight: number;
  angleRadians: number;
  centerX: number;
  centerY: number;
}>;

type VisualEffect = Readonly<{
  kind: "none" | "color_adjust_v1";
  brightnessPermille: number;
  contrastPermille: number;
  saturationPermille: number;
}>;

class VisualCompositorError extends Error {
  constructor(
    readonly blocker: VisualCompositorBlocker,
    message: string,
  ) {
    super(`${blocker}: ${message}`);
    this.name = "VisualCompositorError";
  }
}

function fail(blocker: VisualCompositorBlocker, message: string): never {
  throw new VisualCompositorError(blocker, message);
}

function finiteInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  )
    fail("invalid_contract", `${name} is outside its closed integer range`);
  return value;
}

function numeric(record: NumericRecord, key: string, name: string): number {
  const value = record[key];
  if (typeof value !== "number" || !Number.isFinite(value))
    fail("invalid_contract", `${name}.${key} is not finite`);
  return value;
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function channel(value: number): number {
  return Math.floor(clamp(value) * 255 + 0.5);
}

/** The surface the preview presented before M25-45, and still its floor. */
export const PREVIEW_LEGACY_MAX_SIDE_PX = 320;
export const PREVIEW_LEGACY_MAX_PIXELS = 102_400;
/** The native rung may present the admitted output at its full 1080p raster. */
export const PREVIEW_MAX_SIDE_PX = 1_920;
export const PREVIEW_MAX_PIXELS = 2_073_600;
/**
 * The software rung's own ceiling (implementation record, decision D4).
 *
 * IMPORTANT (M25-45): this is lower than the shipped cap on purpose. The measured matrix puts the
 * software transaction at 26.1 ms p95 for three layers with colour at this size and 60.7 ms at the
 * next step up, against a 50 ms budget -- and its delivered frame interval degrades with it, to
 * 150 ms at the full cap. Raising this number to "look sharper" does not make the fallback
 * sharper; it makes it stop delivering frames. The arbiter is the measured matrix, never a guess.
 */
export const PREVIEW_SOFTWARE_MAX_SIDE_PX = 640;
export const PREVIEW_SOFTWARE_MAX_PIXELS = 230_400;

function scaleWithin(
  width: number,
  height: number,
  maxSide: number,
  maxPixels: number,
): number {
  return Math.min(
    1,
    maxSide / width,
    maxSide / height,
    Math.sqrt(maxPixels / (width * height)),
  );
}

/**
 * The backing store for a composition presented in a measured picture box.
 *
 * IMPORTANT (M25-45): the legacy size is a scale FLOOR, not a separate width and height. Taking
 * the larger of each side independently would stretch a composition whose legacy size is limited
 * by the other side, and a preview that is not the output's aspect ratio is not evidence of
 * anything. The box is in CSS pixels and the backing is in device pixels, so the ratio multiplies
 * the box, never the composition; the composition's own size and the frozen cap are both
 * ceilings, so no path ever upscales. An unmeasured box -- zero, hidden, or with a ratio that is
 * not a positive finite number -- is not a measurement and keeps the floor, which is what makes
 * every 320 x 180 corpus row present at scale 1 in any pane, exactly as before this item.
 */
export function computePreviewSize(
  width: number,
  height: number,
  paneCssWidth = 0,
  paneCssHeight = 0,
  devicePixelRatio = 1,
  path: PreviewPath = "native",
): PreviewSize {
  finiteInteger(width, 16, 1_920, "output width");
  finiteInteger(height, 16, 1_080, "output height");
  if (width % 2 !== 0 || height % 2 !== 0 || width * height > 2_073_600)
    fail(
      "invalid_contract",
      "output dimensions do not match the accepted profile",
    );
  const floor = scaleWithin(
    width,
    height,
    PREVIEW_LEGACY_MAX_SIDE_PX,
    PREVIEW_LEGACY_MAX_PIXELS,
  );
  const ceiling = scaleWithin(
    width,
    height,
    path === "software" ? PREVIEW_SOFTWARE_MAX_SIDE_PX : PREVIEW_MAX_SIDE_PX,
    path === "software" ? PREVIEW_SOFTWARE_MAX_PIXELS : PREVIEW_MAX_PIXELS,
  );
  const measured =
    Number.isFinite(paneCssWidth) &&
    Number.isFinite(paneCssHeight) &&
    paneCssWidth > 0 &&
    paneCssHeight > 0 &&
    Number.isFinite(devicePixelRatio) &&
    devicePixelRatio > 0;
  const requested = measured
    ? Math.min(
        (paneCssWidth * devicePixelRatio) / width,
        (paneCssHeight * devicePixelRatio) / height,
      )
    : 0;
  const scale = Math.min(ceiling, Math.max(floor, Math.min(1, requested)));
  const even = (value: number, limit: number): number =>
    Math.max(2, Math.min(limit, 2 * Math.round(value / 2)));
  const previewWidth = even(width * scale, width);
  const previewHeight = even(height * scale, height);
  // IMPORTANT: validate against the selected rung. Applying the larger native ceiling to the
  // software path defeats its measured frame-time guard and can stall continuous presentation.
  const selectedPixelCeiling =
    path === "software" ? PREVIEW_SOFTWARE_MAX_PIXELS : PREVIEW_MAX_PIXELS;
  if (previewWidth * previewHeight > selectedPixelCeiling)
    fail("resource_limit", "preview surface exceeds the frozen pixel budget");
  return Object.freeze({ width: previewWidth, height: previewHeight, scale });
}

export function computeVisualLayerGeometry(
  nativeWidth: number,
  nativeHeight: number,
  sourceWidth: number,
  sourceHeight: number,
  crop: NumericRecord,
  transform: NumericRecord,
  outputWidth: number,
  outputHeight: number,
): VisualLayerGeometry {
  finiteInteger(nativeWidth, 1, 65_535, "native width");
  finiteInteger(nativeHeight, 1, 65_535, "native height");
  finiteInteger(sourceWidth, 1, 65_535, "source width");
  finiteInteger(sourceHeight, 1, 65_535, "source height");
  finiteInteger(outputWidth, 16, 1_920, "output width");
  finiteInteger(outputHeight, 16, 1_080, "output height");

  const leftBp = finiteInteger(
    numeric(crop, "left_bp", "crop"),
    0,
    9_999,
    "crop.left_bp",
  );
  const topBp = finiteInteger(
    numeric(crop, "top_bp", "crop"),
    0,
    9_999,
    "crop.top_bp",
  );
  const rightBp = finiteInteger(
    numeric(crop, "right_bp", "crop"),
    0,
    9_999,
    "crop.right_bp",
  );
  const bottomBp = finiteInteger(
    numeric(crop, "bottom_bp", "crop"),
    0,
    9_999,
    "crop.bottom_bp",
  );
  if (leftBp + rightBp >= 10_000 || topBp + bottomBp >= 10_000)
    fail("invalid_contract", "crop removes the full source");

  const left = Math.floor((nativeWidth * leftBp) / 10_000);
  const top = Math.floor((nativeHeight * topBp) / 10_000);
  const right = Math.ceil((nativeWidth * (10_000 - rightBp)) / 10_000);
  const bottom = Math.ceil((nativeHeight * (10_000 - bottomBp)) / 10_000);
  const cropWidth = right - left;
  const cropHeight = bottom - top;
  if (cropWidth < 1 || cropHeight < 1)
    fail("invalid_contract", "crop has no source pixels");

  const scaleX = finiteInteger(
    numeric(transform, "scale_x_bp", "transform"),
    1,
    80_000,
    "transform.scale_x_bp",
  );
  const scaleY = finiteInteger(
    numeric(transform, "scale_y_bp", "transform"),
    1,
    80_000,
    "transform.scale_y_bp",
  );
  const scaledWidth = Math.max(
    1,
    Math.floor((cropWidth * scaleX) / 10_000 + 0.5),
  );
  const scaledHeight = Math.max(
    1,
    Math.floor((cropHeight * scaleY) / 10_000 + 0.5),
  );
  if (scaledWidth * scaledHeight * 4 > 1_073_741_824)
    fail(
      "resource_limit",
      "transformed layer exceeds the frozen geometry budget",
    );

  const anchorXBp = finiteInteger(
    numeric(transform, "anchor_x_bp", "transform"),
    0,
    10_000,
    "transform.anchor_x_bp",
  );
  const anchorYBp = finiteInteger(
    numeric(transform, "anchor_y_bp", "transform"),
    0,
    10_000,
    "transform.anchor_y_bp",
  );
  const positionXBp = finiteInteger(
    numeric(transform, "position_x_bp", "transform"),
    -40_000,
    40_000,
    "transform.position_x_bp",
  );
  const positionYBp = finiteInteger(
    numeric(transform, "position_y_bp", "transform"),
    -40_000,
    40_000,
    "transform.position_y_bp",
  );
  const rotationMdeg = finiteInteger(
    numeric(transform, "rotation_mdeg", "transform"),
    -180_000,
    180_000,
    "transform.rotation_mdeg",
  );
  const angleRadians = (rotationMdeg * Math.PI) / 180_000;
  const dx = scaledWidth * (anchorXBp / 10_000 - 0.5);
  const dy = scaledHeight * (anchorYBp / 10_000 - 0.5);
  const rotatedAnchorX =
    dx * Math.cos(angleRadians) - dy * Math.sin(angleRadians);
  const rotatedAnchorY =
    dx * Math.sin(angleRadians) + dy * Math.cos(angleRadians);
  const desiredX = outputWidth / 2 + (outputWidth * positionXBp) / 10_000;
  const desiredY = outputHeight / 2 + (outputHeight * positionYBp) / 10_000;

  return Object.freeze({
    nativeCrop: Object.freeze({
      left,
      top,
      width: cropWidth,
      height: cropHeight,
    }),
    sourceCrop: Object.freeze({
      left: (left * sourceWidth) / nativeWidth,
      top: (top * sourceHeight) / nativeHeight,
      width: (cropWidth * sourceWidth) / nativeWidth,
      height: (cropHeight * sourceHeight) / nativeHeight,
    }),
    scaledWidth,
    scaledHeight,
    angleRadians,
    centerX: desiredX - rotatedAnchorX,
    centerY: desiredY - rotatedAnchorY,
  });
}

function effectValue(
  effect: Readonly<Record<string, number | string>>,
  key: string,
): number {
  const value = effect[key];
  if (typeof value !== "number")
    fail("invalid_contract", `effect.${key} is invalid`);
  return value;
}

/** Two layers share a filter element exactly when their adjustment is the same adjustment. */
function effectKey(effect: VisualEffect): string {
  return `${effect.kind}:${effect.brightnessPermille}:${effect.contrastPermille}:${effect.saturationPermille}`;
}

function decodedEffect(
  effect: Readonly<Record<string, number | string>>,
): VisualEffect {
  const kind = effect.kind;
  if (kind !== "none" && kind !== "color_adjust_v1")
    fail("invalid_contract", "effect kind is unsupported");
  const decoded = Object.freeze({
    kind,
    brightnessPermille: finiteInteger(
      effectValue(effect, "brightnessPermille"),
      -1_000,
      1_000,
      "effect brightness",
    ),
    contrastPermille: finiteInteger(
      effectValue(effect, "contrastPermille"),
      0,
      2_000,
      "effect contrast",
    ),
    saturationPermille: finiteInteger(
      effectValue(effect, "saturationPermille"),
      0,
      2_000,
      "effect saturation",
    ),
  });
  if (
    kind === "none" &&
    (decoded.brightnessPermille !== 0 ||
      decoded.contrastPermille !== 1_000 ||
      decoded.saturationPermille !== 1_000)
  )
    fail("invalid_contract", "none effect is not identity");
  return decoded;
}

function adjustVisualColorInPlace(
  pixels: Uint8ClampedArray,
  effect: VisualEffect,
): Uint8ClampedArray {
  if (pixels.length % 4 !== 0)
    fail("invalid_contract", "RGBA buffer length is invalid");
  const brightness = finiteInteger(
    effect.brightnessPermille,
    -1_000,
    1_000,
    "effect brightness",
  );
  const contrast = finiteInteger(
    effect.contrastPermille,
    0,
    2_000,
    "effect contrast",
  );
  const saturation = finiteInteger(
    effect.saturationPermille,
    0,
    2_000,
    "effect saturation",
  );
  if (effect.kind !== "none" && effect.kind !== "color_adjust_v1")
    fail("invalid_contract", "effect kind is unsupported");
  if (
    effect.kind === "none" &&
    (brightness !== 0 || contrast !== 1_000 || saturation !== 1_000)
  )
    fail("invalid_contract", "none effect is not identity");
  const result = pixels;
  if (
    (effect.kind === "none" || effect.kind === "color_adjust_v1") &&
    brightness === 0 &&
    contrast === 1_000 &&
    saturation === 1_000
  )
    return result;

  // GUARD (M25-20 B-64): the preview predicts the final artifact, and the final artifact is the
  // native renderer's `eq` filter run on 8-bit full-range BT.709 planes. Each plane is an integer
  // between the conversion and the adjustment, and each plane saturates on its own: a chroma
  // plane driven past 255 by a saturation of 2.0 clamps there while the luma stays where it was.
  // A floating-point model that only clamped the reconstructed RGB agreed with the renderer
  // everywhere except in that regime, where it was up to fifteen code values away on a channel
  // (`effect.saturation_permille.upper`, bound twelve) -- the preview showed a picture the final
  // render does not produce. Keep the planes 8-bit and the `eq` arithmetic fixed-point exactly as
  // `semantic_conformance_expect._eq_plane` states them; do not "simplify" this back to floats.
  for (let offset = 0; offset < result.length; offset += 4) {
    const red = result[offset]!;
    const green = result[offset + 1]!;
    const blue = result[offset + 2]!;
    const luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue;
    const lumaPlane = plane8(luma);
    const cbPlane = plane8((blue - luma) / 1.8556 + 128);
    const crPlane = plane8((red - luma) / 1.5748 + 128);
    const adjustedLuma = eqPlane(
      lumaPlane,
      contrast / 1_000,
      brightness / 1_000,
    );
    const adjustedCb = eqPlane(cbPlane, saturation / 1_000, 0);
    const adjustedCr = eqPlane(crPlane, saturation / 1_000, 0);
    const adjustedRed = adjustedLuma + 1.5748 * (adjustedCr - 128);
    const adjustedBlue = adjustedLuma + 1.8556 * (adjustedCb - 128);
    const adjustedGreen =
      adjustedLuma - 0.1873 * (adjustedCb - 128) - 0.4681 * (adjustedCr - 128);
    result[offset] = channel(adjustedRed / 255);
    result[offset + 1] = channel(adjustedGreen / 255);
    result[offset + 2] = channel(adjustedBlue / 255);
  }
  return result;
}

const EQ_CONTRAST_SCALE = 256 * 16;

function plane8(value: number): number {
  // Round half to even, as the oracle's `_clamp8` (Python `round`) does, so an exact .5 lands
  // on the same integer on both sides rather than one code value apart.
  const floor = Math.floor(value);
  const fraction = value - floor;
  const rounded =
    fraction > 0.5 || (fraction === 0.5 && floor % 2 !== 0) ? floor + 1 : floor;
  return Math.max(0, Math.min(255, rounded));
}

/**
 * One 8-bit plane value through the pinned renderer's `eq` filter: the SIMD path's fixed-point
 * arithmetic (`contrast_fixed = (short)(contrast * 256 * 16)`, `brightness_fixed = ((short)(100
 * * brightness + 100) * 511) / 200 - 128 - contrast_fixed / 32`, `((v << 4) * contrast_fixed
 * >> 16) + brightness_fixed`, saturated), with both options first rounded through single
 * precision as the filter rounds them. An identity plane is copied, not adjusted. This is the
 * same function as `semantic_conformance_expect._eq_plane`; the two must move together.
 */
function eqPlane(value: number, contrast: number, brightness: number): number {
  const contrast32 = Math.fround(contrast);
  const brightness32 = Math.fround(brightness);
  if (contrast32 === 1 && brightness32 === 0) return value;
  const contrastFixed = Math.trunc(contrast32 * EQ_CONTRAST_SCALE);
  const percent = Math.trunc(100 * brightness32 + 100);
  const brightnessFixed =
    Math.trunc((percent * 511) / 200) - 128 - Math.trunc(contrastFixed / 32);
  const scaled = ((value << 4) * contrastFixed) >> 16;
  return Math.max(0, Math.min(255, scaled + brightnessFixed));
}

export function applyVisualColorAdjust(
  pixels: Uint8ClampedArray,
  effect: VisualEffect,
): Uint8ClampedArray {
  return adjustVisualColorInPlace(new Uint8ClampedArray(pixels), effect);
}

export function dissolveFactor(
  elapsed: number | null,
  duration: number,
): number {
  finiteInteger(duration, 0, 300, "transition duration");
  if (elapsed === null) return 1;
  if (duration < 1) fail("invalid_contract", "dissolve duration is missing");
  const exactElapsed = finiteInteger(
    elapsed,
    0,
    duration - 1,
    "dissolve elapsed",
  );
  return exactElapsed / duration;
}

export function compositeVisualLayer(
  base: Uint8ClampedArray,
  layer: Uint8ClampedArray,
  opacityBp: number,
  dissolve: number,
  blend: "normal" | "multiply" | "screen",
): void {
  if (base.length !== layer.length || base.length % 4 !== 0)
    fail("invalid_contract", "compositor RGBA buffers do not match");
  const opacity = finiteInteger(opacityBp, 0, 10_000, "opacity") / 10_000;
  if (!Number.isFinite(dissolve) || dissolve < 0 || dissolve > 1)
    fail("invalid_contract", "dissolve factor is invalid");
  if (blend !== "normal" && blend !== "multiply" && blend !== "screen")
    fail("invalid_contract", "blend mode is unsupported");

  for (let offset = 0; offset < base.length; offset += 4) {
    const alpha = (layer[offset + 3]! / 255) * opacity * dissolve;
    if (alpha === 0) {
      base[offset + 3] = 255;
      continue;
    }
    for (let member = 0; member < 3; member += 1) {
      const source = layer[offset + member]! / 255;
      const background = base[offset + member]! / 255;
      const mixed =
        blend === "normal"
          ? source
          : blend === "multiply"
            ? source * background
            : 1 - (1 - source) * (1 - background);
      base[offset + member] = channel(alpha * mixed + (1 - alpha) * background);
    }
    base[offset + 3] = 255;
  }
}

const BLEND_OPERATIONS: Readonly<
  Record<"normal" | "multiply" | "screen", GlobalCompositeOperation>
> = Object.freeze({
  normal: "source-over",
  multiply: "multiply",
  screen: "screen",
});

/**
 * One plane of the pinned `eq` filter as an affine map `value -> value * scale + offset`.
 *
 * IMPORTANT (M25-45): this is `eqPlane` above without its saturating clamp, and the
 * `- contrast_fixed / 32` term is what makes contrast pivot around mid grey. Dropping it moves
 * every channel of every pixel by tens of code values -- it is the whole difference between a
 * filter that matches the renderer within 8 and one that is 163 away (bug register B-M2545-02).
 * Both functions must move together, and with `semantic_conformance_expect._eq_plane`.
 */
function eqPlaneTerms(
  scalePermille: number,
  offsetPermille: number,
): Readonly<{ scale: number; offset: number }> {
  const scale32 = Math.fround(scalePermille / 1_000);
  const offset32 = Math.fround(offsetPermille / 1_000);
  if (scale32 === 1 && offset32 === 0) return { scale: 1, offset: 0 };
  const contrastFixed = Math.trunc(scale32 * EQ_CONTRAST_SCALE);
  const percent = Math.trunc(100 * offset32 + 100);
  return {
    scale: contrastFixed / 4_096,
    offset:
      Math.trunc((percent * 511) / 200) - 128 - Math.trunc(contrastFixed / 32),
  };
}

/**
 * The colour adjustment as the two sRGB `feColorMatrix` stages that the renderer's `eq` is.
 *
 * IMPORTANT (M25-45, bug register B-M2545-22): this cannot be one matrix, however algebraically
 * inviting that is. The renderer runs `eq` on three separate 8-bit planes, and each plane
 * saturates on its own before the inverse colour-space map reads it: a brightness that lifts luma
 * past white leaves the chroma planes where they were, so a bright red comes back (255, 221, 221)
 * and not (255, 255, 255). Composing the planes with both colour-space maps into a single matrix
 * moves that clamp to the very end, after the chroma terms have already been folded in, which
 * states a different picture -- up to 238 code values different, and outside the corpus's
 * `color_adjust_max_abs_channel` across 47% of the declared parameter space.
 *
 * So the first stage carries Y', Cb' and Cr' in the three output channels, where the filter
 * primitive's own clamp to 0..1 *is* the plane saturation, and the second stage is the inverse
 * transform. Measured against `semantic_conformance_expect._adjusted` over the whole declared
 * range, the two stages are within 6 code values and the single matrix was within 238. Do not
 * simplify them back into one.
 */
export function colorAdjustFilterStages(
  brightnessPermille: number,
  contrastPermille: number,
  saturationPermille: number,
): readonly (readonly number[])[] {
  const luma = eqPlaneTerms(contrastPermille, brightnessPermille);
  const chroma = eqPlaneTerms(saturationPermille, 0);
  const toY = [0.2126, 0.7152, 0.0722];
  const toCb = [-0.2126 / 1.8556, -0.7152 / 1.8556, (1 - 0.0722) / 1.8556];
  const toCr = [(1 - 0.2126) / 1.5748, -0.7152 / 1.5748, -0.0722 / 1.5748];
  // `feColorMatrix` works in 0..1; the plane arithmetic is in 0..255. The two chroma planes are
  // centred on 128, so their recentring rides in the constant column.
  const chromaOffset = (128 * chroma.scale + chroma.offset) / 255;
  const planes: number[] = [];
  for (const input of toY) planes.push(luma.scale * input);
  planes.push(0, luma.offset / 255);
  for (const input of toCb) planes.push(chroma.scale * input);
  planes.push(0, chromaOffset);
  for (const input of toCr) planes.push(chroma.scale * input);
  planes.push(0, chromaOffset);
  planes.push(0, 0, 0, 1, 0);

  const fromCb = [0, -0.1873, 1.8556];
  const fromCr = [1.5748, -0.4681, 0];
  const inverse: number[] = [];
  for (let out = 0; out < 3; out += 1)
    inverse.push(
      1,
      fromCb[out]!,
      fromCr[out]!,
      0,
      (-128 * (fromCb[out]! + fromCr[out]!)) / 255,
    );
  inverse.push(0, 0, 0, 1, 0);
  return [planes, inverse];
}

function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const following = value.charCodeAt(index + 1);
      if (!(following >= 0xdc00 && following <= 0xdfff)) return true;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) return true;
  }
  return false;
}

export function validateVisualText(content: string): readonly string[] {
  if (
    typeof content !== "string" ||
    Array.from(content).length < 1 ||
    Array.from(content).length > 2_048 ||
    content.normalize("NFC") !== content ||
    /\r(?!\n)|[\u0085\u2028\u2029]/u.test(content) ||
    /\u0000|[\u0001-\u0008\u000B\u000C\u000E-\u001F]/u.test(content) ||
    hasUnpairedSurrogate(content)
  )
    fail("invalid_contract", "text is outside the closed preview policy");
  const lines = content.split(/\r?\n/u);
  if (lines.length > 32)
    fail("invalid_contract", "text exceeds the preview line budget");
  return Object.freeze(lines);
}

function hasNativeSourceBrand(
  source: CanvasImageSource,
  constructor: unknown,
): boolean {
  if (typeof constructor !== "function") return false;
  try {
    return source instanceof constructor;
  } catch {
    return false;
  }
}

function sourceDimensions(
  kind: "video" | "image",
  source: CanvasImageSource,
): Readonly<{ width: number; height: number }> {
  // CRITICAL: validate the adapter-owned native brand before dimensions, drawImage or readback.
  // Generic CanvasImageSource also admits scriptable HTML/SVG/canvas inputs that can taint pixels.
  const supported =
    kind === "image"
      ? hasNativeSourceBrand(source, globalThis.ImageBitmap)
      : hasNativeSourceBrand(source, globalThis.HTMLVideoElement);
  if (!supported)
    fail(
      "source_unavailable",
      "media source brand is outside the closed policy",
    );
  const candidate = source as unknown as Record<string, unknown>;
  const tag =
    typeof candidate.tagName === "string"
      ? candidate.tagName.toLowerCase()
      : "";
  const constructorName =
    typeof candidate.constructor === "function"
      ? ((candidate.constructor as { name?: string }).name ?? "")
      : "";
  if (tag === "svg" || constructorName.includes("SVG"))
    fail("source_unavailable", "scriptable image sources are unsupported");
  const width =
    candidate.videoWidth ?? candidate.naturalWidth ?? candidate.width;
  const height =
    candidate.videoHeight ?? candidate.naturalHeight ?? candidate.height;
  if (
    typeof width !== "number" ||
    !Number.isSafeInteger(width) ||
    width < 1 ||
    width > 65_535 ||
    typeof height !== "number" ||
    !Number.isSafeInteger(height) ||
    height < 1 ||
    height > 65_535
  )
    fail("source_unavailable", "derivative raster geometry is unavailable");
  return Object.freeze({ width, height });
}

function originalDimension(value: unknown, name: string): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1 ||
    value > 65_535
  )
    fail("source_unavailable", `${name} is unavailable`);
  return value;
}

function rgba(
  value: unknown,
  name: string,
): readonly [number, number, number, number] {
  if (!Array.isArray(value) || value.length !== 4)
    fail("invalid_contract", `${name} is not RGBA8`);
  return Object.freeze(
    value.map((member, index) =>
      finiteInteger(member, 0, 255, `${name}[${index}]`),
    ),
  ) as unknown as readonly [number, number, number, number];
}

function cssRgba(value: readonly [number, number, number, number]): string {
  return `rgba(${value[0]}, ${value[1]}, ${value[2]}, ${value[3] / 255})`;
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function layerFrom(value: Readonly<Record<string, unknown>>): DecodedLayer {
  return value as unknown as DecodedLayer;
}

function textField<T>(text: Readonly<Record<string, unknown>>, key: string): T {
  return text[key] as T;
}

function measureTabbedLine(
  context: CanvasRenderingContext2D,
  line: string,
): Readonly<{
  width: number;
  chunks: readonly Readonly<{ text: string; x: number }>[];
}> {
  const tabWidth = context.measureText(" ").width * 4;
  if (!Number.isFinite(tabWidth) || tabWidth <= 0)
    fail("canvas_unavailable", "packaged font metrics are unavailable");
  const pieces = line.split("\t");
  const chunks: Array<Readonly<{ text: string; x: number }>> = [];
  let cursor = 0;
  for (let index = 0; index < pieces.length; index += 1) {
    const piece = pieces[index]!;
    if (piece.length > 0)
      chunks.push(Object.freeze({ text: piece, x: cursor }));
    cursor += context.measureText(piece).width;
    if (index < pieces.length - 1)
      cursor = (Math.floor(cursor / tabWidth) + 1) * tabWidth;
  }
  return Object.freeze({ width: cursor, chunks: Object.freeze(chunks) });
}

function drawText(
  context: CanvasRenderingContext2D,
  layer: DecodedLayer,
  resource: Extract<VisualLayerResource, { kind: "font" }>,
  geometry: VisualLayerGeometry,
  outputWidth: number,
  outputHeight: number,
  previewScale: number,
): void {
  const text = layer.text;
  if (text === null) fail("invalid_contract", "text layer has no text style");
  if (
    textField<string>(text, "fontAssetId") !== "h3.font.noto_sans.v1" ||
    typeof resource.family !== "string" ||
    resource.family.length < 1 ||
    resource.family.length > 128 ||
    /[\u0000-\u001F\u007F]/u.test(resource.family)
  )
    fail("source_unavailable", "packaged font binding is unavailable");
  const lines = validateVisualText(textField<string>(text, "content"));
  const size = finiteInteger(
    textField<number>(text, "sizePx"),
    8,
    512,
    "text size",
  );
  const weight = finiteInteger(
    textField<number>(text, "weight"),
    400,
    700,
    "text weight",
  );
  if (weight !== 400 && weight !== 700)
    fail("invalid_contract", "text weight is unsupported");
  const style = textField<string>(text, "style");
  const align = textField<string>(text, "align");
  if (
    (style !== "normal" && style !== "italic") ||
    !["left", "center", "right"].includes(align)
  )
    fail("invalid_contract", "text style is unsupported");
  const lineHeightBp = finiteInteger(
    textField<number>(text, "lineHeightBp"),
    7_500,
    30_000,
    "text line height",
  );
  const fill = rgba(text.fillRgba, "text fill");
  const background =
    text.backgroundRgba === null
      ? null
      : rgba(text.backgroundRgba, "text background");

  context.save();
  context.translate(
    geometry.centerX * previewScale,
    geometry.centerY * previewScale,
  );
  context.rotate(geometry.angleRadians);
  context.scale(
    (geometry.scaledWidth / geometry.nativeCrop.width) * previewScale,
    (geometry.scaledHeight / geometry.nativeCrop.height) * previewScale,
  );
  context.translate(
    -(geometry.nativeCrop.left + geometry.nativeCrop.width / 2),
    -(geometry.nativeCrop.top + geometry.nativeCrop.height / 2),
  );
  context.beginPath();
  context.rect(
    geometry.nativeCrop.left,
    geometry.nativeCrop.top,
    geometry.nativeCrop.width,
    geometry.nativeCrop.height,
  );
  context.clip();
  context.font = `${style} ${weight} ${size}px ${JSON.stringify(resource.family)}`;
  context.textAlign = "left";
  context.textBaseline = "alphabetic";
  const lineAdvance = Math.max(1, Math.round((size * lineHeightBp) / 10_000));
  const metric = context.measureText("Mg");
  const ascent = metric.fontBoundingBoxAscent ?? metric.actualBoundingBoxAscent;
  const descent =
    metric.fontBoundingBoxDescent ?? metric.actualBoundingBoxDescent;
  if (!Number.isFinite(ascent) || !Number.isFinite(descent))
    fail("canvas_unavailable", "packaged font bounds are unavailable");
  const top = (outputHeight - lines.length * lineAdvance) / 2;
  context.fillStyle = cssRgba(fill);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!;
    if (line.length === 0) continue;
    const measured = measureTabbedLine(context, line);
    const x =
      align === "left"
        ? 0
        : align === "center"
          ? (outputWidth - measured.width) / 2
          : outputWidth - measured.width;
    const lineTop = top + index * lineAdvance;
    if (background !== null) {
      context.fillStyle = cssRgba(background);
      context.fillRect(x, lineTop, measured.width, lineAdvance);
      context.fillStyle = cssRgba(fill);
    }
    const baseline = lineTop + (lineAdvance + ascent - descent) / 2;
    for (const chunk of measured.chunks)
      context.fillText(chunk.text, x + chunk.x, baseline);
  }
  context.restore();
}

function drawMedia(
  context: CanvasRenderingContext2D,
  resource: Extract<VisualLayerResource, { kind: "video" | "image" }>,
  geometry: VisualLayerGeometry,
  previewScale: number,
): void {
  context.save();
  context.translate(
    geometry.centerX * previewScale,
    geometry.centerY * previewScale,
  );
  context.rotate(geometry.angleRadians);
  context.drawImage(
    resource.source,
    geometry.sourceCrop.left,
    geometry.sourceCrop.top,
    geometry.sourceCrop.width,
    geometry.sourceCrop.height,
    (-geometry.scaledWidth * previewScale) / 2,
    (-geometry.scaledHeight * previewScale) / 2,
    geometry.scaledWidth * previewScale,
    geometry.scaledHeight * previewScale,
  );
  context.restore();
}

function blackPixels(width: number, height: number): Uint8ClampedArray {
  const pixels = new Uint8ClampedArray(width * height * 4);
  for (let offset = 3; offset < pixels.length; offset += 4)
    pixels[offset] = 255;
  return pixels;
}

function outputDimensions(
  snapshot: PublicCompositionSnapshot,
): Readonly<{ width: number; height: number; duration: number }> {
  const width = finiteInteger(
    snapshot.output.width,
    16,
    1_920,
    "snapshot output width",
  );
  const height = finiteInteger(
    snapshot.output.height,
    16,
    1_080,
    "snapshot output height",
  );
  const duration = finiteInteger(
    snapshot.output.durationFrames,
    1,
    1_000_000,
    "snapshot output duration",
  );
  return Object.freeze({ width, height, duration });
}

function expectedDissolve(
  clip: PublicCompositionSnapshot["clips"][number],
  sceneFrame: number,
): Readonly<{ elapsed: number | null; duration: number }> {
  const local = sceneFrame - clip.startFrame;
  const duration = finiteInteger(
    clip.transition.durationFrames,
    0,
    300,
    "transition duration",
  );
  if (clip.transition.kind === "cross_dissolve_v1" && local < duration)
    return Object.freeze({ elapsed: local, duration });
  return Object.freeze({ elapsed: null, duration });
}

function validateScene(
  snapshot: PublicCompositionSnapshot,
  scene: ResolvedCompositionScene,
  resources: VisualLayerResources,
  dimensions: Readonly<{ width: number; height: number; duration: number }>,
): readonly DecodedLayer[] {
  if (
    scene.profileId !== ENGINE_PROFILE_ID ||
    scene.publicFingerprint !== snapshot.publicFingerprint ||
    scene.frame < 0 ||
    scene.frame >= dimensions.duration
  )
    fail("stale_snapshot", "scene does not match the bound snapshot");
  if (scene.blockers.length > 0)
    fail("source_unavailable", "resolved scene contains blockers");
  if (scene.layers.length > 8 || resources.size > 8)
    fail(
      "resource_limit",
      "visual layer or resource count exceeds the frozen limit",
    );

  const clips = new Map(snapshot.clips.map((clip) => [clip.clipId, clip]));
  const tracks = new Map(
    snapshot.tracks.map((track) => [track.trackId, track]),
  );
  const assets = new Map(
    snapshot.assets.map((asset) => [asset.assetId, asset]),
  );
  const kinds = { video: 0, image: 0, text: 0 };
  const expectedClips = snapshot.clips
    .filter((clip) => {
      const track = tracks.get(clip.trackId);
      return (
        clip.enabled &&
        track?.enabled === true &&
        clip.startFrame <= scene.frame &&
        scene.frame < clip.startFrame + clip.durationFrames
      );
    })
    .sort((left, right) => {
      const leftOrder = tracks.get(left.trackId)?.order ?? 0;
      const rightOrder = tracks.get(right.trackId)?.order ?? 0;
      return (
        leftOrder - rightOrder ||
        left.startFrame - right.startFrame ||
        left.clipId.localeCompare(right.clipId)
      );
    });
  if (
    !sameValue(
      scene.layers.map((row) => layerFrom(row).clipId),
      expectedClips.map((clip) => clip.clipId),
    )
  )
    fail(
      "invalid_contract",
      "resolved scene does not contain the exact active layer set",
    );
  let previous: readonly [number, number, string] | null = null;
  const result: DecodedLayer[] = [];
  for (const raw of scene.layers) {
    const layer = layerFrom(raw);
    const clip = clips.get(layer.clipId);
    const track = clip === undefined ? undefined : tracks.get(clip.trackId);
    if (
      clip === undefined ||
      track === undefined ||
      !clip.enabled ||
      !track.enabled ||
      clip.startFrame > scene.frame ||
      clip.startFrame + clip.durationFrames <= scene.frame ||
      layer.trackId !== clip.trackId ||
      layer.assetId !== clip.assetId ||
      layer.opacityBp !== clip.opacityBp ||
      layer.blend !== clip.blend ||
      !sameValue(layer.transform, clip.transform) ||
      !sameValue(layer.crop, clip.crop) ||
      !sameValue(layer.effect, clip.effect) ||
      !sameValue(layer.text, clip.text)
    )
      fail(
        "invalid_contract",
        "resolved layer drifted from the bound snapshot",
      );
    const transition = expectedDissolve(clip, scene.frame);
    if (layer.transitionElapsedFrames !== transition.elapsed)
      fail(
        "invalid_contract",
        "resolved transition drifted from the bound snapshot",
      );
    const expectedOperations = [
      "SelectSourceRangeV1",
      "CropV1",
      "Transform2DV1",
    ];
    if (clip.effect.kind !== "none") expectedOperations.push("ColorAdjustV1");
    expectedOperations.push("OpacityV1", "BlendV1");
    if (transition.elapsed !== null) expectedOperations.push("CrossDissolveV1");
    if (clip.text !== null) expectedOperations.push("DrawTextV1");
    if (!sameValue(layer.operationIds, expectedOperations))
      fail(
        "invalid_contract",
        "resolved operations drifted from the bound snapshot",
      );
    decodedEffect(layer.effect);
    const order: readonly [number, number, string] = [
      track.order,
      clip.startFrame,
      clip.clipId,
    ];
    if (
      previous !== null &&
      (order[0] < previous[0] ||
        (order[0] === previous[0] && order[1] < previous[1]) ||
        (order[0] === previous[0] &&
          order[1] === previous[1] &&
          order[2] < previous[2]))
    )
      fail("invalid_contract", "resolved layer order is not authoritative");
    previous = order;

    const resource = resources.get(layer.clipId);
    if (track.kind === "text_overlay") {
      kinds.text += 1;
      if (resource?.kind !== "font" || layer.text === null)
        fail("source_unavailable", "text resource is unavailable");
      const fontId = textField<string>(layer.text, "fontAssetId");
      if (
        fontId !== "h3.font.noto_sans.v1" ||
        assets.get(fontId)?.kind !== "font" ||
        typeof resource.family !== "string" ||
        resource.family.length < 1 ||
        resource.family.length > 128 ||
        /[\u0000-\u001F\u007F]/u.test(resource.family)
      )
        fail("source_unavailable", "text font asset is not qualified");
      validateVisualText(textField<string>(layer.text, "content"));
      computeVisualLayerGeometry(
        dimensions.width,
        dimensions.height,
        dimensions.width,
        dimensions.height,
        layer.crop,
        layer.transform,
        dimensions.width,
        dimensions.height,
      );
    } else {
      const expectedKind = track.kind === "image_overlay" ? "image" : "video";
      kinds[expectedKind] += 1;
      if (
        resource?.kind !== expectedKind ||
        clip.assetId === null ||
        assets.get(clip.assetId)?.kind !== expectedKind
      )
        fail("source_unavailable", "media resource is unavailable");
      if (
        expectedKind === "video" &&
        (layer.sourceFrame === null || layer.sourcePts === null)
      )
        fail("source_unavailable", "observed video timing is unavailable");
      if (
        expectedKind === "image" &&
        (layer.sourceFrame !== null || layer.sourcePts !== null)
      )
        fail(
          "invalid_contract",
          "image layer unexpectedly carries video timing",
        );
      const nativeWidth = originalDimension(
        resource.nativeWidth,
        "original source width",
      );
      const nativeHeight = originalDimension(
        resource.nativeHeight,
        "original source height",
      );
      const sourceSize = sourceDimensions(resource.kind, resource.source);
      if (sourceSize.width * sourceSize.height > 4_194_304)
        fail(
          "resource_limit",
          "derivative raster exceeds the frozen pixel budget",
        );
      computeVisualLayerGeometry(
        nativeWidth,
        nativeHeight,
        sourceSize.width,
        sourceSize.height,
        layer.crop,
        layer.transform,
        dimensions.width,
        dimensions.height,
      );
    }
    result.push(layer);
  }
  if (kinds.video > 2 || kinds.image > 2 || kinds.text > 4)
    fail("resource_limit", "per-kind visual layer limit exceeded");
  return Object.freeze(result);
}

function blockerFrom(error: unknown): VisualCompositorBlocker {
  return error instanceof VisualCompositorError
    ? error.blocker
    : "canvas_unavailable";
}

/**
 * The owned `<svg>` holding one `<filter>` per distinct colour adjustment in the composition.
 *
 * IMPORTANT (M25-45, bug register B-M2545-01): `url(#id)` resolves by document id, so the host
 * element must never carry a filter's id -- an unresolvable reference makes `context.filter` a
 * silent no-op, which looks exactly like a fast and perfectly accurate filter. The host is
 * appended beside the canvas, inside the extension's own root, and removed on close; no foreign
 * element is created, moved or styled.
 */
function createFilterHost(
  canvas: HTMLCanvasElement,
  key: string,
): SVGSVGElement | null {
  const parent = canvas.parentElement;
  const owner = canvas.ownerDocument;
  if (parent === null || owner === null) return null;
  const host = owner.createElementNS("http://www.w3.org/2000/svg", "svg");
  host.setAttribute("aria-hidden", "true");
  host.setAttribute("width", "0");
  host.setAttribute("height", "0");
  host.setAttribute("data-h3-visual-filters", key);
  host.style.cssText = "position:absolute;width:0;height:0;pointer-events:none";
  parent.append(host);
  return host;
}

/**
 * One filter, built from its stages in order.
 *
 * GUARD: the stages chain implicitly -- a primitive with no `in` reads the previous one's result
 * -- and each result is clamped to 0..1 before the next stage sees it. That clamp is not an
 * artifact to route around; for `colorAdjustFilterStages` it is the renderer's plane saturation.
 * Naming `in`/`result` so a later stage read `SourceGraphic`, or merging the stages, loses it.
 */
function filterElement(
  host: SVGSVGElement,
  id: string,
  stages: readonly (readonly number[])[],
): void {
  const owner = host.ownerDocument;
  const filter = owner.createElementNS("http://www.w3.org/2000/svg", "filter");
  filter.setAttribute("id", id);
  filter.setAttribute("color-interpolation-filters", "sRGB");
  for (const values of stages) {
    const matrix = owner.createElementNS(
      "http://www.w3.org/2000/svg",
      "feColorMatrix",
    );
    matrix.setAttribute("type", "matrix");
    matrix.setAttribute(
      "values",
      values.map((value) => value.toFixed(6)).join(" "),
    );
    filter.append(matrix);
  }
  host.append(filter);
}

export function createVisualCompositor(
  canvas: HTMLCanvasElement,
  snapshot: PublicCompositionSnapshot,
  box: PreviewBox | null = null,
): VisualCompositor {
  if (snapshot.profileId !== ENGINE_PROFILE_ID)
    fail("invalid_contract", "snapshot profile is unsupported");
  let dimensions = outputDimensions(snapshot);
  let path: PreviewPath = "software";
  let measured: PreviewBox | null = box;
  const sizeFor = (): PreviewSize =>
    computePreviewSize(
      dimensions.width,
      dimensions.height,
      measured?.cssWidth ?? 0,
      measured?.cssHeight ?? 0,
      measured?.devicePixelRatio ?? 1,
      path,
    );
  let preview = sizeFor();
  canvas.width = preview.width;
  canvas.height = preview.height;
  let context: CanvasRenderingContext2D | null = null;

  const isContextLost = (candidate: CanvasRenderingContext2D): boolean => {
    const checker = (candidate as unknown as { isContextLost?: () => boolean })
      .isContextLost;
    if (typeof checker !== "function") return false;
    try {
      return checker.call(candidate) === true;
    } catch {
      return true;
    }
  };

  const acquireContext = (): CanvasRenderingContext2D | null => {
    try {
      const candidate = canvas.getContext("2d", {
        alpha: true,
        colorSpace: "srgb",
        willReadFrequently: true,
      });
      if (candidate === null || isContextLost(candidate)) return null;
      candidate.imageSmoothingEnabled = true;
      // IMPORTANT: the pinned Canvas2D preview profile uses native bilinear resampling;
      // changing this hint selects a different browser filter and invalidates pixel evidence.
      candidate.imageSmoothingQuality = "low";
      candidate.filter = "none";
      return candidate;
    } catch {
      return null;
    }
  };

  const requireContext = (): CanvasRenderingContext2D => {
    if (context !== null && !isContextLost(context)) return context;
    context = acquireContext();
    if (context === null) fail("canvas_unavailable", "Canvas2D is unavailable");
    return context;
  };

  context = acquireContext();

  let closed = false;
  let rendering = false;
  let latestGeneration = -1;
  let latestFrame: number | null = null;
  let latestLayerCount = 0;
  let latestGeometries = new Map<string, VisualLayerGeometry>();

  const receipt = (
    status: VisualCompositorReceipt["status"],
    blocker: VisualCompositorReceipt["blocker"],
    generation: number,
    frame: number | null,
    renderedLayerCount: number,
  ): VisualCompositorReceipt =>
    Object.freeze({
      schema: VISUAL_COMPOSITOR_RECEIPT_SCHEMA,
      status,
      profileId: ENGINE_PROFILE_ID,
      publicFingerprint: snapshot.publicFingerprint,
      frame,
      generation,
      previewWidth: preview.width,
      previewHeight: preview.height,
      renderedLayerCount,
      blocker,
      browserPreviewOnly: true,
    });

  const clearBlack = (): void => {
    const activeContext = requireContext();
    activeContext.save();
    activeContext.setTransform(1, 0, 0, 1, 0, 0);
    activeContext.globalAlpha = 1;
    activeContext.globalCompositeOperation = "source-over";
    // Explicit, because this also runs from the failure path, where the state that was in effect
    // when the frame threw is whatever it happened to be.
    activeContext.filter = "none";
    activeContext.fillStyle = "#000000";
    activeContext.fillRect(0, 0, preview.width, preview.height);
    activeContext.restore();
  };

  try {
    clearBlack();
  } catch {
    context = null;
  }

  // The frozen eligibility table. A composition renders natively when every enabled clip's effect
  // is one the engine itself can apply: the identity, or a colour adjustment the filter rung has
  // been probed to reproduce. Anything else -- a future effect kind, or a browser whose filter
  // does not apply -- takes the incumbent software transaction whole, at its own lower cap.
  //
  // IMPORTANT (M25-45): the corpus is the arbiter of this table, not the clock. A rung is
  // eligible because its picture matched the software path within the frozen tolerance on every
  // measured cell (implementation record D1, D2), and the semantic corpus re-proves it on the
  // shipped path. Never widen this table because a class "should" be safe or because the fast
  // path is faster: a rung that renders a different picture is not an optimization, it is a
  // silent divergence between the preview and the final render that M25-20 exists to prevent.
  const effectsFor = (subject: PublicCompositionSnapshot) => {
    const effects = new Map<string, VisualEffect>();
    for (const clip of subject.clips) {
      if (!clip.enabled) continue;
      const effect = decodedEffect(clip.effect);
      if (effect.kind !== "none") effects.set(effectKey(effect), effect);
    }
    return effects;
  };
  let colorAdjustments = effectsFor(snapshot);

  const filterKey = `${snapshot.publicFingerprint.slice(-16)}-${Math.trunc(
    Math.random() * 0xffffffff,
  ).toString(16)}`;
  let filterHost: SVGSVGElement | null = null;
  const filterIds = new Map<string, string>();
  let filterProven = false;

  const installFilters = () => {
    if (filterHost === null) return;
    for (const [key, effect] of colorAdjustments) {
      const id = `h3-visual-filter-${filterKey}-${filterIds.size}`;
      filterIds.set(key, id);
      filterElement(
        filterHost,
        id,
        colorAdjustFilterStages(
          effect.brightnessPermille,
          effect.contrastPermille,
          effect.saturationPermille,
        ),
      );
    }
  };

  const probeFilter = (): boolean => {
    if (context === null) return false;
    filterHost ??= createFilterHost(canvas, filterKey);
    if (filterHost === null) return false;
    const probeId = `h3-visual-filter-${filterKey}-probe`;
    // A matrix that sends every input to opaque red: the probe fails closed unless the filter
    // demonstrably changed the pixel, because an unresolvable reference is silently ignored.
    filterElement(filterHost, probeId, [
      [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
    ]);
    installFilters();
    try {
      const active = requireContext();
      // IMPORTANT: a lazy filter probe during an edit must restore the held pixel synchronously.
      // Clearing/resizing or leaving the probe's red pixel visible breaks retained presentation.
      const heldPixel =
        latestFrame === null ? null : active.getImageData(0, 0, 1, 1);
      active.save();
      try {
        active.setTransform(1, 0, 0, 1, 0, 0);
        active.globalAlpha = 1;
        active.globalCompositeOperation = "source-over";
        active.filter = `url(#${probeId})`;
        if (active.filter !== `url(#${probeId})`) return false;
        active.fillStyle = "#000000";
        active.fillRect(0, 0, 1, 1);
        const [red, green, blue, alpha] = active.getImageData(0, 0, 1, 1).data;
        return red === 255 && green === 0 && blue === 0 && alpha === 255;
      } finally {
        active.restore();
        if (heldPixel !== null) active.putImageData(heldPixel, 0, 0);
      }
    } catch {
      return false;
    }
  };

  const dropFilters = (): void => {
    filterIds.clear();
    if (filterHost !== null) {
      filterHost.remove();
      filterHost = null;
    }
  };

  const filterAvailable = colorAdjustments.size === 0 ? true : probeFilter();
  filterProven = colorAdjustments.size > 0 && filterAvailable;
  path = context !== null && filterAvailable ? "native" : "software";
  if (!filterAvailable) dropFilters();
  preview = sizeFor();
  canvas.width = preview.width;
  canvas.height = preview.height;
  try {
    clearBlack();
  } catch {
    context = null;
  }

  const render = (
    scene: ResolvedCompositionScene,
    resources: VisualLayerResources,
    generation: number,
  ): VisualCompositorReceipt => {
    finiteInteger(
      generation,
      0,
      Number.MAX_SAFE_INTEGER,
      "compositor generation",
    );
    if (closed) return receipt("closed", "closed", generation, null, 0);
    if (generation < latestGeneration)
      return receipt(
        "unavailable",
        "stale_snapshot",
        generation,
        scene.frame,
        0,
      );
    if (rendering) {
      // CRITICAL: the visible canvas is also the sole scratch surface. Re-entry would expose a
      // partially cleared layer as a successful frame and violate the one-surface transaction.
      return receipt(
        "unavailable",
        "resource_limit",
        generation,
        scene.frame,
        0,
      );
    }
    latestGeneration = generation;
    rendering = true;
    try {
      const activeContext = requireContext();
      const layers = validateScene(snapshot, scene, resources, dimensions);
      const frameGeometries = new Map<string, VisualLayerGeometry>();
      const paint = (
        layer: (typeof layers)[number],
        resource: VisualLayerResource,
      ): void => {
        if (resource.kind === "font") {
          const geometry = computeVisualLayerGeometry(
            dimensions.width,
            dimensions.height,
            dimensions.width,
            dimensions.height,
            layer.crop,
            layer.transform,
            dimensions.width,
            dimensions.height,
          );
          frameGeometries.set(layer.clipId, geometry);
          drawText(
            activeContext,
            layer,
            resource,
            geometry,
            dimensions.width,
            dimensions.height,
            preview.scale,
          );
          return;
        }
        const nativeWidth = originalDimension(
          resource.nativeWidth,
          "original source width",
        );
        const nativeHeight = originalDimension(
          resource.nativeHeight,
          "original source height",
        );
        const sourceSize = sourceDimensions(resource.kind, resource.source);
        if (sourceSize.width * sourceSize.height > 4_194_304)
          fail(
            "resource_limit",
            "derivative raster exceeds the frozen pixel budget",
          );
        const geometry = computeVisualLayerGeometry(
          nativeWidth,
          nativeHeight,
          sourceSize.width,
          sourceSize.height,
          layer.crop,
          layer.transform,
          dimensions.width,
          dimensions.height,
        );
        frameGeometries.set(layer.clipId, geometry);
        drawMedia(activeContext, resource, geometry, preview.scale);
      };
      const transitionOf = (clipId: string): number => {
        const clip = snapshot.clips.find(
          (candidate) => candidate.clipId === clipId,
        );
        if (clip === undefined)
          fail("invalid_contract", "layer clip is unavailable");
        return clip.transition.durationFrames;
      };

      if (path === "native") {
        // The engine's own alpha, blend and colour filter, back to front, onto the one canvas.
        // No readback, no scratch buffer and no second surface: the frame either completes or the
        // catch below repaints the fail-closed placeholder.
        clearBlack();
        for (const layer of layers) {
          const resource = resources.get(layer.clipId);
          if (resource === undefined)
            fail("source_unavailable", "layer resource is unavailable");
          const effect = decodedEffect(layer.effect);
          const filterId =
            effect.kind === "none"
              ? null
              : (filterIds.get(effectKey(effect)) ?? null);
          // A layer whose effect has no probed filter never renders without its effect: the frame
          // fails closed instead, because a preview silently missing a colour adjustment is the
          // exact divergence this rung must not introduce.
          if (effect.kind !== "none" && filterId === null)
            fail("invalid_contract", "layer effect has no probed filter");
          activeContext.save();
          activeContext.setTransform(1, 0, 0, 1, 0, 0);
          activeContext.globalAlpha =
            (layer.opacityBp / 10_000) *
            dissolveFactor(
              layer.transitionElapsedFrames,
              transitionOf(layer.clipId),
            );
          activeContext.globalCompositeOperation =
            BLEND_OPERATIONS[layer.blend];
          activeContext.filter =
            filterId === null ? "none" : `url(#${filterId})`;
          paint(layer, resource);
          activeContext.restore();
        }
        latestFrame = scene.frame;
        latestLayerCount = layers.length;
        latestGeometries = frameGeometries;
        return receipt(
          "presented",
          null,
          generation,
          scene.frame,
          layers.length,
        );
      }

      const accumulator = blackPixels(preview.width, preview.height);
      for (const layer of layers) {
        const resource = resources.get(layer.clipId);
        if (resource === undefined)
          fail("source_unavailable", "layer resource is unavailable");
        activeContext.save();
        activeContext.setTransform(1, 0, 0, 1, 0, 0);
        activeContext.globalAlpha = 1;
        activeContext.globalCompositeOperation = "source-over";
        activeContext.filter = "none";
        activeContext.clearRect(0, 0, preview.width, preview.height);
        activeContext.restore();

        const transitionDuration = transitionOf(layer.clipId);
        paint(layer, resource);
        const pixels = activeContext.getImageData(
          0,
          0,
          preview.width,
          preview.height,
        ).data;
        const adjusted = adjustVisualColorInPlace(
          pixels,
          decodedEffect(layer.effect),
        );
        compositeVisualLayer(
          accumulator,
          adjusted,
          layer.opacityBp,
          dissolveFactor(layer.transitionElapsedFrames, transitionDuration),
          layer.blend,
        );
      }
      const output = activeContext.createImageData(
        preview.width,
        preview.height,
      );
      output.data.set(accumulator);
      activeContext.putImageData(output, 0, 0);
      latestFrame = scene.frame;
      latestLayerCount = layers.length;
      latestGeometries = frameGeometries;
      return receipt("presented", null, generation, scene.frame, layers.length);
    } catch (error) {
      const blocker = blockerFrom(error);
      if (blocker === "canvas_unavailable") context = null;
      try {
        clearBlack();
      } catch {
        // The receipt remains fail-closed even when a lost context cannot accept the placeholder.
        context = null;
      }
      latestFrame = null;
      latestLayerCount = 0;
      latestGeometries.clear();
      return receipt("unavailable", blocker, generation, scene.frame, 0);
    } finally {
      rendering = false;
    }
  };

  const clear = (
    generation = Math.max(0, latestGeneration),
  ): VisualCompositorReceipt => {
    finiteInteger(
      generation,
      0,
      Number.MAX_SAFE_INTEGER,
      "compositor generation",
    );
    if (closed) return receipt("closed", "closed", generation, null, 0);
    if (generation < latestGeneration)
      return receipt(
        "unavailable",
        "stale_snapshot",
        generation,
        latestFrame,
        latestLayerCount,
      );
    latestGeneration = generation;
    latestFrame = null;
    latestLayerCount = 0;
    latestGeometries.clear();
    try {
      clearBlack();
    } catch {
      context = null;
    }
    return receipt("unavailable", "canvas_unavailable", generation, null, 0);
  };

  const close = (): VisualCompositorReceipt => {
    if (!closed) {
      closed = true;
      try {
        clearBlack();
      } catch {
        context = null;
      }
      dropFilters();
      latestFrame = null;
      latestLayerCount = 0;
      latestGeometries.clear();
    }
    return receipt("closed", "closed", Math.max(0, latestGeneration), null, 0);
  };

  /**
   * Re-set the backing store for a newly measured picture box.
   *
   * IMPORTANT (M25-45): it paints no old pixels. Resizing a canvas discards its contents, and a
   * frame reconstructed from a stale observation after a seek, a replace or a revoked lease is a
   * receipt for something the session is no longer showing. The caller re-renders the current
   * frame; an unchanged quantized size does no work at all, so a stream of identical observations
   * costs nothing.
   */
  const resize = (nextBox: PreviewBox): VisualCompositorReceipt => {
    if (closed)
      return receipt(
        "closed",
        "closed",
        Math.max(0, latestGeneration),
        null,
        0,
      );
    measured = nextBox;
    const next = sizeFor();
    if (next.width === preview.width && next.height === preview.height)
      return receipt(
        "unavailable",
        "stale_snapshot",
        Math.max(0, latestGeneration),
        latestFrame,
        latestLayerCount,
      );
    preview = next;
    canvas.width = preview.width;
    canvas.height = preview.height;
    latestFrame = null;
    latestLayerCount = 0;
    latestGeometries.clear();
    try {
      // A backing reset drops every context state the profile pins, so the full state is
      // reapplied before anything is drawn.
      context = acquireContext();
      clearBlack();
    } catch {
      context = null;
    }
    return receipt(
      "unavailable",
      "canvas_unavailable",
      Math.max(0, latestGeneration),
      null,
      0,
    );
  };

  const replace = (next: PublicCompositionSnapshot): boolean => {
    if (closed || rendering || context === null || isContextLost(context))
      return false;
    try {
      const nextDimensions = outputDimensions(next);
      if (
        next.profileId !== snapshot.profileId ||
        next.workspaceHandle !== snapshot.workspaceHandle ||
        nextDimensions.width !== dimensions.width ||
        nextDimensions.height !== dimensions.height
      )
        return false;
      const nextEffects = effectsFor(next);
      // IMPORTANT: keep the admitted raster rung. A capability/path change needs the full reset
      // path; changing caps in place would claim a frame on a different backing store.
      if (path === "software" && nextEffects.size === 0) return false;
      if (path === "native" && nextEffects.size > 0) {
        if (
          filterHost !== null &&
          filterHost.parentElement !== canvas.parentElement
        )
          return false;
        colorAdjustments = nextEffects;
        if (!filterProven) {
          filterProven = probeFilter();
          if (!filterProven) {
            dropFilters();
            return false;
          }
        }
        filterHost ??= createFilterHost(canvas, filterKey);
        if (filterHost === null) return false;
        // Replace the bounded current set; per-revision filter caches grow without an owner bound.
        filterHost.replaceChildren();
        filterIds.clear();
        installFilters();
      } else {
        colorAdjustments = nextEffects;
        dropFilters();
      }
      snapshot = next;
      dimensions = nextDimensions;
      return true;
    } catch {
      return false;
    }
  };

  return Object.freeze({
    render,
    clear,
    close,
    resize,
    replace,
    path: (): PreviewPath => path,
    layerGeometry: (clipId: string): VisualLayerGeometry | null =>
      latestGeometries.get(clipId) ?? null,
  });
}
