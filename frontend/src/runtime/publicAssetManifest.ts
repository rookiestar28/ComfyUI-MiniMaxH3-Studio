import { sha256Text } from "../contracts/canonicalFingerprint";
import {
  ENGINE_PROFILE_ID,
  type PublicCompositionSnapshot,
} from "../contracts/compositionCodec";
import { RUNTIME_PROFILE_FINGERPRINT } from "./mediaCapabilities";

export const PUBLIC_ASSET_MANIFEST_SCHEMA =
  "h3.context.public_media_asset_manifest.v1" as const;

export type PublicRuntimeAsset = Readonly<{
  assetId: string;
  kind: "video" | "image" | "font";
  sourceTimeBase: Readonly<{ num: number; den: number }> | null;
  sourceFrameCount: number | null;
  sourceSampleCount: number | null;
  embeddedAudio:
    "present_bound" | "absent" | "unavailable" | "excluded_overlay_policy";
  timestampPolicy: "nonnegative_monotonic_v1" | "not_applicable";
  landmarks: readonly Readonly<{
    frameIndex: number;
    pts: number;
    dts: number;
    durationTicks: number;
  }>[];
}>;

export type PublicAssetManifest = Readonly<{
  schema: typeof PUBLIC_ASSET_MANIFEST_SCHEMA;
  profileId: typeof ENGINE_PROFILE_ID;
  profileFingerprint: typeof RUNTIME_PROFILE_FINGERPRINT;
  publicFingerprint: string;
  workspaceRevision: number;
  timelineRevision: number;
  assets: readonly PublicRuntimeAsset[];
  manifestFingerprint: string;
}>;

type ManifestPayload = Omit<PublicAssetManifest, "manifestFingerprint">;

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function manifestPayload(snapshot: PublicCompositionSnapshot): ManifestPayload {
  return {
    schema: PUBLIC_ASSET_MANIFEST_SCHEMA,
    profileId: ENGINE_PROFILE_ID,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    publicFingerprint: snapshot.publicFingerprint,
    workspaceRevision: snapshot.workspaceRevision,
    timelineRevision: snapshot.timelineRevision,
    assets: snapshot.assets.map((asset) => ({
      assetId: asset.assetId,
      kind: asset.kind,
      sourceTimeBase:
        asset.sourceTimeBase === null ? null : { ...asset.sourceTimeBase },
      sourceFrameCount: asset.sourceFrameCount,
      sourceSampleCount: asset.sourceSampleCount,
      embeddedAudio: asset.embeddedAudio,
      timestampPolicy: asset.timestampPolicy,
      landmarks: asset.landmarks.map((landmark) => ({ ...landmark })),
    })),
  };
}

export function buildPublicAssetManifest(
  snapshot: PublicCompositionSnapshot,
): PublicAssetManifest {
  const payload = manifestPayload(snapshot);
  return deepFreeze({
    ...payload,
    manifestFingerprint: sha256Text(canonicalJson(payload)),
  });
}

export function validatePublicAssetManifest(
  manifest: PublicAssetManifest,
  snapshot: PublicCompositionSnapshot,
): PublicAssetManifest {
  const expected = buildPublicAssetManifest(snapshot);
  if (canonicalJson(manifest) !== canonicalJson(expected))
    throw new Error(
      "contract_mismatch: public asset manifest does not bind the snapshot",
    );
  return manifest;
}

export function publicAssetById(
  manifest: PublicAssetManifest,
  assetId: string,
): PublicRuntimeAsset | undefined {
  return manifest.assets.find((asset) => asset.assetId === assetId);
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value as Record<string, unknown>))
      deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}
