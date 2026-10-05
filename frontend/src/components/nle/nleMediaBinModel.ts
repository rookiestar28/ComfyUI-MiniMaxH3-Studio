import type { PublicCompositionAsset } from "../../contracts/compositionCodec";
import { formatTimelineTimecode } from "../../runtime/timelineNavigation";

export type NleMediaBinProjection = Readonly<{
  asset: PublicCompositionAsset;
  ordinal: number;
  label: string;
}>;

export type MediaFilter = "all" | "video" | "image" | "added";
export type MediaSort = "ordinal_asc" | "ordinal_desc";

/** Search only the public ordinal card label, never an asset identifier or private source path. */
export function selectMediaBinProjection(
  rows: readonly NleMediaBinProjection[],
  input: Readonly<{
    query: string;
    filter: MediaFilter;
    sort: MediaSort;
    addedAssetIds: ReadonlySet<string>;
    cardTemplate: string;
  }>,
): readonly NleMediaBinProjection[] {
  const query = input.query.trim().toLocaleLowerCase();
  const filtered = rows.filter(({ asset, ordinal }) => {
    if (input.filter === "added" && !input.addedAssetIds.has(asset.assetId))
      return false;
    if (
      (input.filter === "video" || input.filter === "image") &&
      asset.kind !== input.filter
    )
      return false;
    const displayedLabel = input.cardTemplate.replace(
      "{ordinal}",
      String(ordinal).padStart(2, "0"),
    );
    return displayedLabel.toLocaleLowerCase().includes(query);
  });
  return input.sort === "ordinal_desc" ? filtered.reverse() : filtered;
}

type FrameRate = Readonly<{ num: number; den: number }>;

function validRate(value: FrameRate | null): value is FrameRate {
  return (
    value !== null &&
    Number.isSafeInteger(value.num) &&
    Number.isSafeInteger(value.den) &&
    value.num > 0 &&
    value.den > 0
  );
}

export function mediaBinProjection(
  assets: readonly PublicCompositionAsset[],
): readonly NleMediaBinProjection[] {
  return assets
    .filter(
      (asset): asset is PublicCompositionAsset & { kind: "video" | "image" } =>
        asset.kind === "video" || asset.kind === "image",
    )
    .map((asset, index) =>
      Object.freeze({
        asset,
        ordinal: index + 1,
        label: `Clip ${String(index + 1).padStart(2, "0")}`,
      }),
    );
}

export function mediaDurationTimecode(
  asset: PublicCompositionAsset,
  frameRate: FrameRate | null,
): string | null {
  if (!validRate(frameRate)) return null;
  const durationFrames = mediaInsertionDurationFrames(asset, frameRate);
  if (durationFrames === null) return null;
  return formatTimelineTimecode(durationFrames, frameRate.num / frameRate.den);
}

/**
 * M25-63: the duration pill's compact form, `MM:SS` (or `H:MM:SS`), or whole frames below one
 * second (`1f` for a still), as the ruler labels sub-second marks. The full timecode stays in the
 * pill's tooltip and in the card's accessible name.
 */
export function mediaDurationPill(
  asset: PublicCompositionAsset,
  frameRate: FrameRate | null,
): string | null {
  if (!validRate(frameRate)) return null;
  const frames = mediaInsertionDurationFrames(asset, frameRate);
  if (frames === null) return null;
  const seconds = Math.floor((frames * frameRate.den) / frameRate.num);
  if (seconds < 1) return `${frames}f`;
  const pad = (value: number) => String(value).padStart(2, "0");
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = `${pad(minutes)}:${pad(seconds % 60)}`;
  return hours > 0 ? `${hours}:${rest}` : rest;
}

export function mediaInsertionDurationFrames(
  asset: PublicCompositionAsset,
  frameRate: FrameRate | null,
): number | null {
  if (asset.kind === "image") return validRate(frameRate) ? 1 : null;
  if (
    asset.kind !== "video" ||
    asset.sourceFrameCount === null ||
    asset.sourceFrameCount < 1 ||
    asset.sourceTimeBase === null ||
    !Number.isSafeInteger(asset.sourceTimeBase.num) ||
    !Number.isSafeInteger(asset.sourceTimeBase.den) ||
    asset.sourceTimeBase.num < 1 ||
    asset.sourceTimeBase.den < 1 ||
    !validRate(frameRate)
  )
    return null;
  const first = asset.landmarks[0];
  const last = asset.landmarks.at(-1);
  if (first === undefined || last === undefined || first.frameIndex !== 0)
    return null;
  const sourceTicks =
    BigInt(last.pts) + BigInt(last.durationTicks) - BigInt(first.pts);
  if (sourceTicks < 1n) return null;
  // CRITICAL: sourceFrameCount is a source identity count, not an output duration. Reusing it
  // across frame rates overclaims the source interval and the core rejects the insert with
  // source_range_unavailable.
  const numerator =
    sourceTicks * BigInt(asset.sourceTimeBase.num) * BigInt(frameRate.num);
  const denominator = BigInt(asset.sourceTimeBase.den) * BigInt(frameRate.den);
  const frames = numerator / denominator;
  if (frames < 1n || frames > BigInt(Number.MAX_SAFE_INTEGER)) return null;
  return Number(frames);
}

export function virtualMediaRange(
  input: Readonly<{
    itemCount: number;
    viewportWidth: number;
    viewportHeight: number;
    scrollTop: number;
    minimumCardWidth: number;
    rowHeight: number;
  }>,
): Readonly<{
  columns: number;
  visibleRows: number;
  startIndex: number;
  endIndex: number;
  topSpacerPx: number;
  bottomSpacerPx: number;
  mountedLimit: number;
}> {
  const columns = Math.max(
    1,
    Math.floor(
      Math.max(0, input.viewportWidth) / Math.max(1, input.minimumCardWidth),
    ),
  );
  const rowHeight = Math.max(1, input.rowHeight);
  const visibleRows = Math.max(
    1,
    Math.ceil(Math.max(0, input.viewportHeight) / rowHeight),
  );
  const totalRows = Math.ceil(Math.max(0, input.itemCount) / columns);
  const firstVisibleRow = Math.min(
    Math.max(0, totalRows - 1),
    Math.floor(Math.max(0, input.scrollTop) / rowHeight),
  );
  const startRow = Math.max(0, firstVisibleRow - 1);
  const endRow = Math.min(totalRows, firstVisibleRow + visibleRows + 1);
  return Object.freeze({
    columns,
    visibleRows,
    startIndex: Math.min(input.itemCount, startRow * columns),
    endIndex: Math.min(input.itemCount, endRow * columns),
    topSpacerPx: startRow * rowHeight,
    bottomSpacerPx: Math.max(0, totalRows - endRow) * rowHeight,
    mountedLimit: columns * (visibleRows + 2),
  });
}
