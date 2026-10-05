import {
  AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
  canonicalPublicRuntimeAssetFingerprint,
} from "../contracts/authoringMediaLeaseCodec";
import {
  buildAuthoringPreparationLeaseRequest,
  type NleAuthoringMediaAssetPreparationContext,
} from "../runtime/authoringDecorationLeaseRequest";
import { RUNTIME_PROFILE } from "../runtime/mediaCapabilities";
import {
  nleAuthoringAssetById,
  validateNleAuthoringAssetManifest,
} from "../runtime/nleAuthoringAssetManifest";
import { AuthoringMediaSourceLeaseError } from "./authoringMediaLeaseTransport";
import type {
  AuthoringMediaSourceLease,
  AuthoringMediaSourceLeaseClient,
} from "./authoringMediaSourceLease";

/** What a preparation leaves behind: no bytes in the page, only the fact and a lease to end. */
export type AuthoringMediaPreparedPlayback = Readonly<{
  value: Readonly<{
    derivativeKind: "video_proxy" | "audio_preview";
    cacheKey: Readonly<{
      workspaceHandle: string;
      assetFingerprint: string;
      derivativeProfileId: string;
      derivativeKind: "video_proxy" | "audio_preview";
    }>;
  }>;
  release(): Promise<void>;
}>;

/**
 * The body of the lease client's `prepareAssetPlayback`: the service generates a catalog asset's
 * playback derivative before a clip uses the asset. It creates through the client it is given
 * and owns no transport of its own. It is a module beside the client because the client's module
 * is held to the host module line budget.
 */
export async function prepareAuthoringAssetPlayback(
  client: Pick<AuthoringMediaSourceLeaseClient, "create">,
  requestId: () => string,
  context: NleAuthoringMediaAssetPreparationContext,
  signal: AbortSignal,
): Promise<AuthoringMediaPreparedPlayback> {
  try {
    validateNleAuthoringAssetManifest(context.manifest, context.authoring);
  } catch {
    throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
  }
  const asset = nleAuthoringAssetById(context.manifest, context.assetId);
  if (
    asset === undefined ||
    asset.kind !== "video" ||
    asset.sourceFrameCount === null ||
    asset.landmarks.length !== asset.sourceFrameCount ||
    (context.derivativeKind === "audio_preview" &&
      (asset.sourceSampleCount === null ||
        asset.embeddedAudio !== "present_bound"))
  )
    throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
  if (signal.aborted)
    throw new AuthoringMediaSourceLeaseError("cancelled", 499);
  const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
  let lease: AuthoringMediaSourceLease;
  try {
    // IMPORTANT: a preparation is one create and nothing else. The service keeps the body
    // it generated for the clip that asks next; opening the lease here would download bytes
    // this page has no use for and hold the service's single worker while it does.
    lease = await client.create(
      buildAuthoringPreparationLeaseRequest(
        context,
        requestId(),
        asset.sourceFrameCount,
      ),
      signal,
      assetFingerprint,
    );
  } catch (error) {
    if (error instanceof AuthoringMediaSourceLeaseError) throw error;
    throw new AuthoringMediaSourceLeaseError(
      signal.aborted ? "cancelled" : "internal_failure",
      signal.aborted ? 499 : 0,
    );
  }
  return Object.freeze({
    value: Object.freeze({
      derivativeKind: context.derivativeKind,
      cacheKey: Object.freeze({
        workspaceHandle: context.authoring.workspaceHandle,
        assetFingerprint,
        derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
        derivativeKind: context.derivativeKind,
      }),
    }),
    // A failed release leaves the lease registered, so the client's `close()` ends it later.
    release: () =>
      lease.release(
        AbortSignal.timeout(RUNTIME_PROFILE.limits.teardownDeadlineMs),
      ),
  });
}
