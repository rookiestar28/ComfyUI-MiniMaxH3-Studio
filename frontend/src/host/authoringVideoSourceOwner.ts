import {
  AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
  canonicalPublicRuntimeAssetFingerprint,
  type AuthoringMediaGeometry,
  type AuthoringMediaLeaseCreateRequest,
} from "../contracts/authoringMediaLeaseCodec";
import type { AuthoringMediaVideoSourceContext } from "../runtime/authoringDecorationLeaseRequest";
import type {
  HtmlMediaElementSourceOwner,
  MediaTransportOpen,
} from "../runtime/editorRuntime";
import { RUNTIME_PROFILE } from "../runtime/mediaCapabilities";
import {
  publicAssetById,
  validatePublicAssetManifest,
} from "../runtime/publicAssetManifest";
import { AuthoringMediaSourceLeaseError } from "./authoringMediaLeaseTransport";
import type {
  AuthoringMediaLeaseBody,
  AuthoringMediaSourceLease,
  AuthoringMediaSourceLeaseClient,
} from "./authoringMediaSourceLease";

type Dependencies = Readonly<{
  client: Pick<AuthoringMediaSourceLeaseClient, "create">;
  requestId(): string;
  createVideoElement(): HTMLVideoElement;
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
  waitForMetadata(
    element: HTMLVideoElement,
    signal: AbortSignal,
  ): Promise<void>;
  teardown(element: HTMLVideoElement): void;
}>;

function admission(
  request: MediaTransportOpen,
  context: AuthoringMediaVideoSourceContext,
  requestId: () => string,
) {
  validatePublicAssetManifest(context.manifest, context.snapshot);
  const clip = context.snapshot.clips.find(
    (value) => value.clipId === context.clipId,
  );
  const asset = publicAssetById(context.manifest, request.asset.assetId);
  if (
    clip === undefined ||
    clip.clipId !== request.ownerId ||
    clip.assetId !== request.asset.assetId ||
    asset === undefined ||
    canonicalPublicRuntimeAssetFingerprint(asset) !==
      canonicalPublicRuntimeAssetFingerprint(request.asset) ||
    asset.kind !== "video" ||
    asset.sourceFrameCount === null ||
    context.sourceStartFrame !== 0 ||
    context.sourceEndFrame !== asset.sourceFrameCount
  )
    throw new AuthoringMediaSourceLeaseError("contract_mismatch", 0);
  const track = context.snapshot.tracks.find(
    (candidate) => candidate.trackId === clip.trackId,
  );
  const needsAudio =
    asset.embeddedAudio === "present_bound" &&
    clip.enabled &&
    track?.kind === "primary_video" &&
    track.enabled;
  const create = (
    derivativeKind: "video_proxy" | "audio_preview",
  ): AuthoringMediaLeaseCreateRequest => ({
    schema: AUTHORING_MEDIA_LEASE_REQUEST_SCHEMA,
    operation: "create",
    requestId: requestId(),
    workspaceHandle: context.snapshot.workspaceHandle,
    workspaceRevision: context.snapshot.workspaceRevision,
    timelineRevision: context.snapshot.timelineRevision,
    publicFingerprint: context.snapshot.publicFingerprint,
    manifestFingerprint: context.manifest.manifestFingerprint,
    profileFingerprint: context.manifest.profileFingerprint,
    scope: "clip",
    clipId: context.clipId,
    assetId: asset.assetId,
    derivativeKind,
    ownerId: request.ownerId,
    runtimeEpoch: request.epoch,
    sourceStartFrame: 0,
    sourceEndFrame: asset.sourceFrameCount!,
  });
  return {
    needsAudio,
    create,
    fingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
  };
}

function matches(
  lease: AuthoringMediaSourceLease,
  body: AuthoringMediaLeaseBody,
): boolean {
  if (
    typeof lease.derivative !== "function" ||
    typeof lease.adopt !== "function"
  )
    return false;
  const next = lease.derivative();
  // CRITICAL: video identity alone cannot authorize retained PCM. Compare each freshly admitted
  // derivative with its own initially verified body, including kind and byte count, before adopt.
  return (
    next.fingerprint === body.receipt.derivativeFingerprint &&
    next.byteCount === body.blob.size &&
    next.kind === body.receipt.derivativeKind
  );
}

export async function acquireAuthoringVideoSource(
  dependencies: Dependencies,
  request: MediaTransportOpen,
  context: AuthoringMediaVideoSourceContext,
): Promise<
  HtmlMediaElementSourceOwner &
    Readonly<{ geometry: AuthoringMediaGeometry | null }>
> {
  const first = admission(request, context, dependencies.requestId);
  let videoLease: AuthoringMediaSourceLease | undefined;
  let audioLease: AuthoringMediaSourceLease | undefined;
  let videoBody: AuthoringMediaLeaseBody | undefined;
  let audioBody: AuthoringMediaLeaseBody | undefined;
  let element: HTMLVideoElement | undefined;
  let url: string | undefined;
  const authorities = new Set<AuthoringMediaSourceLease>();
  async function releaseAuthorities(
    leases: readonly (AuthoringMediaSourceLease | undefined)[],
  ) {
    const results = await Promise.allSettled(
      [...new Set(leases)]
        .filter(
          (lease): lease is AuthoringMediaSourceLease => lease !== undefined,
        )
        .map(async (lease) => {
          await lease.release(
            AbortSignal.timeout(RUNTIME_PROFILE.limits.teardownDeadlineMs),
          );
          authorities.delete(lease);
        }),
    );
    const failed = results.filter((result) => result.status === "rejected");
    if (failed.length === 1) throw failed[0]!.reason;
    if (failed.length > 1)
      throw new AggregateError(
        failed.map((result) => result.reason),
        "media owner authority release failed",
      );
  }
  async function create(
    next: ReturnType<typeof admission>,
    kind: "video_proxy" | "audio_preview",
    signal: AbortSignal,
  ) {
    const lease = await dependencies.client.create(
      next.create(kind),
      signal,
      next.fingerprint,
    );
    authorities.add(lease);
    return lease;
  }
  try {
    videoLease = await create(first, "video_proxy", request.signal);
    videoBody = await videoLease.open(request.signal);
    if (first.needsAudio) {
      audioLease = await create(first, "audio_preview", request.signal);
      audioBody = await audioLease.open(request.signal);
    }
    element = dependencies.createVideoElement();
    url = dependencies.createObjectURL(videoBody.blob);
    element.preload = "auto";
    element.src = url;
    await dependencies.waitForMetadata(element, request.signal);
  } catch (error) {
    if (element !== undefined) dependencies.teardown(element);
    if (url !== undefined) dependencies.revokeObjectURL(url);
    await releaseAuthorities([audioLease, videoLease]).catch(() => undefined);
    throw error instanceof AuthoringMediaSourceLeaseError
      ? error
      : new AuthoringMediaSourceLeaseError(
          request.signal.aborted ? "cancelled" : "internal_failure",
          request.signal.aborted ? 499 : 0,
        );
  }
  const video = element;
  const heldVideo = videoBody;
  const heldAudio = audioBody;
  const heldUrl = url;
  let localRevoked = false;
  let failed = false;
  let rebinding = false;
  let suspended = false;
  let released = false;
  let releasePromise: Promise<void> | undefined;
  let subscriptions: (() => void)[] = [];
  const unsubscribe = () => {
    for (const end of subscriptions) end();
    subscriptions = [];
  };
  const subscribe = () => {
    subscriptions = [videoLease, audioLease]
      .filter(
        (lease): lease is AuthoringMediaSourceLease => lease !== undefined,
      )
      .map((lease) =>
        lease.subscribeFailure(() => {
          // IMPORTANT: a queued retired failure may run after unsubscribe. It must not revoke the
          // decoder holding the replacement lease; the old authority is also suspended during rebind.
          if (
            localRevoked ||
            suspended ||
            (lease !== videoLease && lease !== audioLease)
          )
            return;
          failed = true;
          video.dispatchEvent(new Event("error"));
        }),
      );
  };
  subscribe();
  return Object.freeze({
    element: video,
    audioBody: heldAudio?.blob,
    geometry: heldVideo.geometry,
    async rebind(
      nextRequest: MediaTransportOpen,
      nextContext?: AuthoringMediaVideoSourceContext,
    ) {
      if (
        localRevoked ||
        failed ||
        rebinding ||
        nextRequest.signal.aborted ||
        nextRequest.ownerId !== request.ownerId ||
        nextContext === undefined ||
        nextContext.snapshot.workspaceHandle !==
          context.snapshot.workspaceHandle ||
        nextContext.manifest.profileFingerprint !==
          context.manifest.profileFingerprint ||
        canonicalPublicRuntimeAssetFingerprint(nextRequest.asset) !==
          first.fingerprint
      )
        return false;
      const next = admission(nextRequest, nextContext, dependencies.requestId);
      if (next.needsAudio !== (heldAudio !== undefined)) return false;
      rebinding = true;
      suspended = true;
      let freshVideo: AuthoringMediaSourceLease | undefined;
      let freshAudio: AuthoringMediaSourceLease | undefined;
      let adopted = false;
      let primaryFailure = false;
      try {
        freshVideo = await create(next, "video_proxy", nextRequest.signal);
        if (localRevoked || nextRequest.signal.aborted) return false;
        if (heldAudio !== undefined)
          freshAudio = await create(next, "audio_preview", nextRequest.signal);
        if (
          localRevoked ||
          nextRequest.signal.aborted ||
          !matches(freshVideo, heldVideo) ||
          (heldAudio !== undefined &&
            (freshAudio === undefined || !matches(freshAudio, heldAudio)))
        )
          return false;
        freshVideo.adopt!();
        freshAudio?.adopt!();
        const retiredVideo = videoLease;
        const retiredAudio = audioLease;
        unsubscribe();
        videoLease = freshVideo;
        audioLease = freshAudio;
        adopted = true;
        suspended = false;
        subscribe();
        await releaseAuthorities([retiredAudio, retiredVideo]);
        return !localRevoked && !failed && !nextRequest.signal.aborted;
      } catch (error) {
        primaryFailure = true;
        throw error;
      } finally {
        rebinding = false;
        suspended = false;
        if (!adopted) {
          try {
            await releaseAuthorities([freshAudio, freshVideo]);
          } catch (error) {
            // IMPORTANT: cleanup must preserve the acquisition refusal. Failed authority stays
            // registered for owner/client retry; replacing stale with busy hides the real blocker.
            if (!primaryFailure) throw error;
          }
        }
      }
    },
    async release() {
      if (released) return;
      if (releasePromise !== undefined) return releasePromise;
      // IMPORTANT: revoke locally once, but keep every failed authority registered and retryable.
      // A transport failure is not proof of release, including an old lease retired by rebind.
      if (!localRevoked) {
        localRevoked = true;
        unsubscribe();
        dependencies.revokeObjectURL(heldUrl);
      }
      releasePromise = releaseAuthorities([
        audioLease,
        videoLease,
        ...authorities,
      ]).then(
        () => {
          released = true;
        },
        (error: unknown) => {
          releasePromise = undefined;
          throw error;
        },
      );
      return releasePromise;
    },
  });
}
