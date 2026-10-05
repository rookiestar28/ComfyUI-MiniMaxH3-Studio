import {
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
  type AuthoringMediaLeaseCreateRequest,
  type NleAuthoringAssetLeaseCreateRequest,
} from "../contracts/authoringMediaLeaseCodec";
import type { NleAuthoringStateV2 } from "../contracts/authoringWorkbenchCodec";
import type { PublicCompositionSnapshot } from "../contracts/compositionCodec";
import type { NleAuthoringAssetManifest } from "./nleAuthoringAssetManifest";
import type { PublicAssetManifest } from "./publicAssetManifest";

export type AuthoringMediaVideoSourceContext = Readonly<{
  snapshot: PublicCompositionSnapshot;
  manifest: PublicAssetManifest;
  clipId: string;
  sourceStartFrame: number;
  sourceEndFrame: number;
}>;

export type AuthoringMediaAssetDecorationContextV1 = Readonly<{
  snapshot: PublicCompositionSnapshot;
  manifest: PublicAssetManifest;
  assetId: string;
  ownerId: string;
  runtimeEpoch: number;
  derivativeKind: "thumbnail" | "filmstrip" | "audio_peaks";
}>;

export type NleAuthoringMediaAssetDecorationContext = Readonly<{
  authoring: NleAuthoringStateV2;
  manifest: NleAuthoringAssetManifest;
  assetId: string;
  ownerId: string;
  runtimeEpoch: number;
  derivativeKind: "thumbnail" | "filmstrip" | "audio_peaks";
}>;

export type AuthoringMediaAssetDecorationContext =
  | AuthoringMediaAssetDecorationContextV1
  | NleAuthoringMediaAssetDecorationContext;

/** A catalog asset whose playback derivative is prepared before a clip uses the asset. */
export type NleAuthoringMediaAssetPreparationContext = Readonly<{
  authoring: NleAuthoringStateV2;
  manifest: NleAuthoringAssetManifest;
  assetId: string;
  ownerId: string;
  runtimeEpoch: number;
  derivativeKind: "video_proxy" | "audio_preview";
}>;

function nleAuthoringAssetLeaseRequest(
  context:
    | NleAuthoringMediaAssetDecorationContext
    | NleAuthoringMediaAssetPreparationContext,
  requestId: string,
  sourceEndFrame: number,
): NleAuthoringAssetLeaseCreateRequest {
  return {
    schema: NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
    operation: "create",
    requestId,
    authoringSchema: context.authoring.schema,
    profileId: context.authoring.profileId,
    workspaceHandle: context.authoring.workspaceHandle,
    workspaceRevision: context.authoring.workspaceRevision,
    timelineRevision: context.authoring.timelineRevision,
    authoringFingerprint: context.authoring.authoringFingerprint,
    manifestFingerprint: context.manifest.manifestFingerprint,
    profileFingerprint: context.manifest.profileFingerprint,
    assetId: context.assetId,
    derivativeKind: context.derivativeKind,
    ownerId: context.ownerId,
    runtimeEpoch: context.runtimeEpoch,
    sourceStartFrame: 0,
    sourceEndFrame,
  };
}

export function buildAuthoringDecorationLeaseRequest(
  context: AuthoringMediaAssetDecorationContext,
  requestId: string,
  sourceEndFrame: number,
): AuthoringMediaLeaseCreateRequest | NleAuthoringAssetLeaseCreateRequest {
  if ("authoring" in context)
    return nleAuthoringAssetLeaseRequest(context, requestId, sourceEndFrame);
  return {
    schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
    operation: "create",
    requestId,
    workspaceHandle: context.snapshot.workspaceHandle,
    workspaceRevision: context.snapshot.workspaceRevision,
    timelineRevision: context.snapshot.timelineRevision,
    publicFingerprint: context.snapshot.publicFingerprint,
    manifestFingerprint: context.manifest.manifestFingerprint,
    profileFingerprint: context.manifest.profileFingerprint,
    scope: "asset",
    clipId: null,
    assetId: context.assetId,
    derivativeKind: context.derivativeKind,
    ownerId: context.ownerId,
    runtimeEpoch: context.runtimeEpoch,
    sourceStartFrame: 0,
    sourceEndFrame,
  };
}

/** A preparation names the whole source, which is the span the clip that uses it will ask for. */
export function buildAuthoringPreparationLeaseRequest(
  context: NleAuthoringMediaAssetPreparationContext,
  requestId: string,
  sourceFrameCount: number,
): NleAuthoringAssetLeaseCreateRequest {
  return nleAuthoringAssetLeaseRequest(context, requestId, sourceFrameCount);
}
