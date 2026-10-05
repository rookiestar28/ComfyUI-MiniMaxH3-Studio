import type { PublicRuntimeAsset } from "../runtime/publicAssetManifest";

export const AUTHORING_FILMSTRIP_MEDIA_TYPE = "image/jpeg" as const;
export const AUTHORING_FILMSTRIP_MAX_BYTES = 512 * 1024;
export const AUTHORING_FILMSTRIP_TILE_HEIGHT = 48;
export const AUTHORING_FILMSTRIP_MIN_TILES = 4;
export const AUTHORING_FILMSTRIP_MAX_TILES = 64;

export function authoringFilmstripTileCount(asset: PublicRuntimeAsset): number {
  // Composition timing landmarks are sparse by contract; frame count is not landmark count.
  if (
    asset.kind !== "video" ||
    asset.sourceTimeBase === null ||
    asset.sourceFrameCount === null ||
    asset.landmarks.length < 1
  )
    throw new Error("filmstrip asset is invalid");
  const last = asset.landmarks.at(-1)!;
  const ticks = BigInt(last.pts) + BigInt(last.durationTicks);
  const numerator = 2n * ticks * BigInt(asset.sourceTimeBase.num);
  const denominator = BigInt(asset.sourceTimeBase.den);
  const requested = Number((numerator + denominator - 1n) / denominator);
  return Math.max(
    AUTHORING_FILMSTRIP_MIN_TILES,
    Math.min(AUTHORING_FILMSTRIP_MAX_TILES, requested),
  );
}
