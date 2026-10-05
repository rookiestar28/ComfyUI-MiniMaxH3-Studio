export type FilmstripPaintAsset = Readonly<{
  assetId: string;
  kind: "video";
  sourceWidth: number;
  sourceHeight: number;
  sourceTimeBase: Readonly<{ num: number; den: number }>;
  sourceFrameCount: number;
  landmarks: readonly Readonly<{
    frameIndex: number;
    pts: number;
    dts: number;
    durationTicks: number;
  }>[];
}>;

export type FilmstripPaintInput = Readonly<{
  asset: FilmstripPaintAsset;
  /** The timeline's rate: `clip.durationFrames` counts output frames at it (B-M2563-05). */
  outputFrameRate: Readonly<{ num: number; den: number }>;
  clip: Readonly<{ sourceStartFrame: number; durationFrames: number }>;
  bounds: Readonly<{ x: number; y: number; width: number; height: number }>;
  visible: Readonly<{ x: number; width: number }>;
  sprite: Readonly<{
    bitmap: ImageBitmap;
    width: number;
    height: number;
    tileCount: number;
  }>;
}>;

export type FilmstripPaintCell = Readonly<{
  sourceTile: number;
  source: Readonly<{ x: number; y: number; width: number; height: number }>;
  destination: Readonly<{
    x: number;
    y: number;
    width: number;
    height: number;
  }>;
}>;

const finitePositive = (value: number): boolean =>
  Number.isFinite(value) && value > 0;

function filmstripSourceTickAtFrame(
  asset: FilmstripPaintAsset,
  sourceFrame: number,
  durationTicks: number,
): number {
  const landmarks = asset.landmarks;
  let lower = landmarks[0]!;
  for (let index = 1; index < landmarks.length; index += 1) {
    const upper = landmarks[index]!;
    if (sourceFrame === upper.frameIndex)
      return Math.min(durationTicks, upper.pts);
    if (sourceFrame < upper.frameIndex) {
      const frameSpan = BigInt(upper.frameIndex - lower.frameIndex);
      const frameOffset = BigInt(sourceFrame - lower.frameIndex);
      const tickSpan = BigInt(upper.pts - lower.pts);
      const roundedOffset =
        (tickSpan * frameOffset * 2n + frameSpan) / (frameSpan * 2n);
      return Math.min(durationTicks, lower.pts + Number(roundedOffset));
    }
    lower = upper;
  }

  // Sparse public timing landmarks need not include every frame or the source tail. Reuse the
  // measured final extent rather than inventing timestamps beyond the last admitted anchor.
  return durationTicks;
}

/**
 * IMPORTANT (B-M2563-05): the source tick an output frame shows, by the core's rule
 * (`composition_contract._source_target_tick`): the start landmark's pts plus the elapsed output
 * time converted into the source's time base, floored. `sourceStartFrame` is a source identity
 * and `elapsed` is output time; adding the two, as this painter once did, is only right when the
 * source runs at the timeline's rate. A start that is not a landmark of a sparse table takes the
 * interpolated tick, as before.
 */
function filmstripSourceTickAtOutput(
  asset: FilmstripPaintAsset,
  outputFrameRate: Readonly<{ num: number; den: number }>,
  sourceStartFrame: number,
  elapsed: number,
  durationTicks: number,
): number {
  const start =
    asset.landmarks.find((landmark) => landmark.frameIndex === sourceStartFrame)
      ?.pts ??
    filmstripSourceTickAtFrame(asset, sourceStartFrame, durationTicks);
  const offset =
    (BigInt(elapsed) *
      BigInt(outputFrameRate.den) *
      BigInt(asset.sourceTimeBase.den)) /
    (BigInt(outputFrameRate.num) * BigInt(asset.sourceTimeBase.num));
  return Math.min(durationTicks, start + Number(offset));
}

export function planFilmstripCells(
  input: FilmstripPaintInput,
): readonly FilmstripPaintCell[] {
  const { asset, bounds, clip, outputFrameRate, sprite, visible } = input;
  const invalidFields: string[] = [];
  if (asset.kind !== "video") invalidFields.push("asset.kind");
  if (
    !Number.isSafeInteger(asset.sourceWidth) ||
    !Number.isSafeInteger(asset.sourceHeight) ||
    asset.sourceWidth < 1 ||
    asset.sourceHeight < 1
  )
    invalidFields.push("asset.dimensions");
  if (
    !Number.isSafeInteger(asset.sourceFrameCount) ||
    asset.sourceFrameCount < 1 ||
    asset.landmarks.length < 1 ||
    asset.landmarks[0]?.frameIndex !== 0 ||
    asset.landmarks.some(
      (landmark, index) =>
        !Number.isSafeInteger(landmark.frameIndex) ||
        landmark.frameIndex < 0 ||
        landmark.frameIndex >= asset.sourceFrameCount ||
        !Number.isSafeInteger(landmark.pts) ||
        landmark.pts < 0 ||
        !Number.isSafeInteger(landmark.durationTicks) ||
        landmark.durationTicks < 1 ||
        (index > 0 &&
          (landmark.frameIndex <= asset.landmarks[index - 1]!.frameIndex ||
            landmark.pts <= asset.landmarks[index - 1]!.pts)),
    )
  )
    invalidFields.push("asset.frame-landmarks");
  const positiveRational = (
    rate: Readonly<{ num: number; den: number }> | undefined,
  ) =>
    rate !== undefined &&
    Number.isSafeInteger(rate.num) &&
    Number.isSafeInteger(rate.den) &&
    rate.num >= 1 &&
    rate.den >= 1;
  if (!positiveRational(asset.sourceTimeBase))
    invalidFields.push("asset.time-base");
  // Output time past the source's end shows its last frame, as the render path does; only a
  // start outside the source is out of range here.
  if (
    !Number.isSafeInteger(clip.sourceStartFrame) ||
    !Number.isSafeInteger(clip.durationFrames) ||
    clip.sourceStartFrame < 0 ||
    clip.durationFrames < 1 ||
    clip.sourceStartFrame >= asset.sourceFrameCount
  )
    invalidFields.push("clip.source-range");
  if (!positiveRational(outputFrameRate))
    invalidFields.push("clip.output-rate");
  if (
    !finitePositive(bounds.width) ||
    !finitePositive(bounds.height) ||
    !finitePositive(visible.width)
  )
    invalidFields.push("paint.geometry");
  if (
    !Number.isSafeInteger(sprite.tileCount) ||
    sprite.tileCount < 4 ||
    sprite.tileCount > 64 ||
    sprite.height !== 48 ||
    !Number.isSafeInteger(sprite.width) ||
    sprite.width < sprite.tileCount ||
    sprite.width % sprite.tileCount !== 0
  )
    invalidFields.push("sprite.geometry");
  if (invalidFields.length > 0)
    throw new Error(
      `filmstrip paint input is invalid: ${invalidFields.join(",")}`,
    );

  const clipLeft = bounds.x;
  const clipRight = bounds.x + bounds.width;
  const visibleLeft = Math.max(clipLeft, visible.x);
  const visibleRight = Math.min(clipRight, visible.x + visible.width);
  if (visibleRight <= visibleLeft) return [];
  const destinationCellWidth =
    (bounds.height * asset.sourceWidth) / asset.sourceHeight;
  if (!finitePositive(destinationCellWidth))
    throw new Error("filmstrip cell geometry is invalid");
  const sourceCellWidth = sprite.width / sprite.tileCount;
  const last = asset.landmarks.at(-1)!;
  const durationTicks = last.pts + last.durationTicks;
  if (!Number.isSafeInteger(durationTicks) || durationTicks < 1)
    throw new Error("filmstrip source duration is invalid");

  const cells: FilmstripPaintCell[] = [];
  const cellCount = Math.ceil(bounds.width / destinationCellWidth);
  for (let index = 0; index < cellCount; index += 1) {
    const fullLeft = clipLeft + index * destinationCellWidth;
    const fullRight = Math.min(clipRight, fullLeft + destinationCellWidth);
    const left = Math.max(fullLeft, visibleLeft);
    const right = Math.min(fullRight, visibleRight);
    if (right <= left) continue;
    const centre = (fullLeft + fullRight) / 2;
    const outputFrame = Math.min(
      clip.durationFrames - 1,
      Math.max(
        0,
        Math.floor(((centre - clipLeft) / bounds.width) * clip.durationFrames),
      ),
    );
    const sourceTick = filmstripSourceTickAtOutput(
      asset,
      outputFrameRate,
      clip.sourceStartFrame,
      outputFrame,
      durationTicks,
    );
    const sourceTile = Math.min(
      sprite.tileCount - 1,
      Math.max(0, Math.round((sourceTick * sprite.tileCount) / durationTicks)),
    );
    const leftFraction = (left - fullLeft) / destinationCellWidth;
    const rightFraction = (right - fullLeft) / destinationCellWidth;
    cells.push(
      Object.freeze({
        sourceTile,
        source: Object.freeze({
          x: sourceTile * sourceCellWidth + leftFraction * sourceCellWidth,
          y: 0,
          width: (rightFraction - leftFraction) * sourceCellWidth,
          height: sprite.height,
        }),
        destination: Object.freeze({
          x: left,
          y: bounds.y,
          width: right - left,
          height: bounds.height,
        }),
      }),
    );
  }
  return Object.freeze(cells);
}

export function paintFilmstrip(
  context: CanvasRenderingContext2D,
  input: FilmstripPaintInput | null,
): void {
  if (input === null) return;
  for (const cell of planFilmstripCells(input))
    context.drawImage(
      input.sprite.bitmap,
      cell.source.x,
      cell.source.y,
      cell.source.width,
      cell.source.height,
      cell.destination.x,
      cell.destination.y,
      cell.destination.width,
      cell.destination.height,
    );
}
