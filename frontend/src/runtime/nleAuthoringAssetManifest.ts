import { sha256Text } from "../contracts/canonicalFingerprint";
import {
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  type NleAuthoringStateV2,
} from "../contracts/authoringWorkbenchCodec";
import type { PublicCompositionAsset } from "../contracts/compositionCodec";
import { RUNTIME_PROFILE_FINGERPRINT } from "./mediaCapabilities";

export const NLE_AUTHORING_ASSET_MANIFEST_SCHEMA =
  "h3.context.nle_asset_manifest.v1" as const;

export type NleAuthoringAssetManifest = Readonly<{
  schema: typeof NLE_AUTHORING_ASSET_MANIFEST_SCHEMA;
  authoringSchema: typeof NLE_AUTHORING_SCHEMA;
  profileId: typeof NLE_AUTHORING_PROFILE_ID;
  profileFingerprint: typeof RUNTIME_PROFILE_FINGERPRINT;
  workspaceHandle: string;
  workspaceRevision: number;
  timelineRevision: number;
  authoringFingerprint: string;
  assets: readonly PublicCompositionAsset[];
  manifestFingerprint: string;
}>;

type ManifestPayload = Omit<NleAuthoringAssetManifest, "manifestFingerprint">;

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

function payload(authoring: NleAuthoringStateV2): ManifestPayload {
  return {
    schema: NLE_AUTHORING_ASSET_MANIFEST_SCHEMA,
    authoringSchema: authoring.schema,
    profileId: authoring.profileId,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    workspaceHandle: authoring.workspaceHandle,
    workspaceRevision: authoring.workspaceRevision,
    timelineRevision: authoring.timelineRevision,
    authoringFingerprint: authoring.authoringFingerprint,
    assets: authoring.assets.map((asset) => ({
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

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value as Record<string, unknown>))
      deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

export function buildNleAuthoringAssetManifest(
  authoring: NleAuthoringStateV2,
): NleAuthoringAssetManifest {
  const value = payload(authoring);
  return deepFreeze({
    ...value,
    manifestFingerprint: sha256Text(canonicalJson(value)),
  });
}

export function validateNleAuthoringAssetManifest(
  manifest: NleAuthoringAssetManifest,
  authoring: NleAuthoringStateV2,
): NleAuthoringAssetManifest {
  const expected = buildNleAuthoringAssetManifest(authoring);
  if (canonicalJson(manifest) !== canonicalJson(expected))
    throw new Error(
      "contract_mismatch: NLE asset manifest does not bind authoring state",
    );
  return manifest;
}

export function nleAuthoringAssetById(
  manifest: NleAuthoringAssetManifest,
  assetId: string,
): PublicCompositionAsset | undefined {
  return manifest.assets.find((asset) => asset.assetId === assetId);
}
