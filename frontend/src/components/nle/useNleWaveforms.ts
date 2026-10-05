import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { AuthoringAudioPeaks } from "../../contracts/authoringAudioPeaks";
import {
  AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
  canonicalPublicRuntimeAssetFingerprint,
} from "../../contracts/authoringMediaLeaseCodec";
import type { PublicCompositionSnapshot } from "../../contracts/compositionCodec";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaDecorationLeaseClient,
  AuthoringMediaSourceLeaseClient,
} from "../../host/authoringMediaSourceLease";
import { createNleAudioPeaksCache } from "../../host/nleAudioPeaksCache";
import type { NleDecorationDemandBroker } from "../../host/nleDecorationDemandBroker";
import { buildPublicAssetManifest } from "../../runtime/publicAssetManifest";

export function useNleWaveforms({
  snapshot,
  runtimeEpoch,
  leaseClient,
  decorationDemandBroker,
  visibleAssetIds,
}: Readonly<{
  snapshot: PublicCompositionSnapshot;
  runtimeEpoch: number;
  leaseClient?: AuthoringMediaSourceLeaseClient;
  decorationDemandBroker?: NleDecorationDemandBroker<
    AuthoringMediaAssetDecoration["value"]
  >;
  visibleAssetIds: readonly string[];
}>): ReadonlyMap<string, AuthoringAudioPeaks> {
  const manifest = useMemo(
    () => buildPublicAssetManifest(snapshot),
    [snapshot],
  );
  const fingerprints = useMemo(
    () =>
      new Map(
        manifest.assets.map((asset) => [
          asset.assetId,
          canonicalPublicRuntimeAssetFingerprint(asset),
        ]),
      ),
    [manifest],
  );
  const eligibleAssets = useMemo(() => {
    const primaryTracks = new Set(
      snapshot.tracks
        .filter((track) => track.kind === "primary_video" && track.enabled)
        .map((track) => track.trackId),
    );
    return new Set(
      snapshot.clips
        .filter(
          (clip) =>
            clip.enabled &&
            clip.assetId !== null &&
            primaryTracks.has(clip.trackId),
        )
        .map((clip) => clip.assetId!),
    );
  }, [snapshot]);
  const cache = useMemo(() => createNleAudioPeaksCache(), []);
  const [cacheRevision, setCacheRevision] = useState(0);
  const priorAuthority = useRef({
    workspaceHandle: snapshot.workspaceHandle,
    runtimeEpoch,
  });
  const publish = useCallback(
    (value: AuthoringMediaAssetDecoration["value"]) => {
      if (value.derivativeKind !== "audio_peaks") {
        value.bitmap.close();
        return;
      }
      cache.set(value.cacheKey, value.peaks);
      setCacheRevision((current) => current + 1);
    },
    [cache],
  );
  const source = useMemo(
    () =>
      decorationDemandBroker?.register(
        "nle-timeline-waveforms",
        "audio_peaks",
        publish,
      ) ?? null,
    [decorationDemandBroker, publish],
  );

  useLayoutEffect(() => {
    const workspaceChanged =
      priorAuthority.current.workspaceHandle !== snapshot.workspaceHandle;
    const ownerChanged = priorAuthority.current.runtimeEpoch !== runtimeEpoch;
    if (workspaceChanged)
      cache.purgeWorkspace(priorAuthority.current.workspaceHandle);
    else if (ownerChanged) cache.purgeWorkspace(snapshot.workspaceHandle);
    cache.retainWorkspaceAssets(
      snapshot.workspaceHandle,
      new Set(fingerprints.values()),
    );
    priorAuthority.current = {
      workspaceHandle: snapshot.workspaceHandle,
      runtimeEpoch,
    };
    if (workspaceChanged || ownerChanged)
      setCacheRevision((current) => current + 1);
  }, [cache, fingerprints, runtimeEpoch, snapshot.workspaceHandle]);

  useEffect(() => {
    if (source === null) return;
    if (leaseClient === undefined) {
      source.update([]);
      return;
    }
    const client =
      "acquireAssetDecoration" in leaseClient
        ? (leaseClient as AuthoringMediaDecorationLeaseClient)
        : null;
    const seen = new Set<string>();
    const demands =
      client === null
        ? []
        : visibleAssetIds.flatMap((assetId, ordinal) => {
            if (seen.has(assetId) || !eligibleAssets.has(assetId)) return [];
            seen.add(assetId);
            const asset = manifest.assets.find(
              (item) => item.assetId === assetId,
            );
            const assetFingerprint = fingerprints.get(assetId);
            if (
              asset?.kind !== "video" ||
              asset.embeddedAudio !== "present_bound" ||
              asset.sourceFrameCount === null ||
              asset.sourceSampleCount === null ||
              assetFingerprint === undefined ||
              cache.get({
                workspaceHandle: snapshot.workspaceHandle,
                assetFingerprint,
                derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
                derivativeKind: "audio_peaks",
              }) !== undefined
            )
              return [];
            return [
              {
                key: `${snapshot.workspaceHandle}:${assetFingerprint}`,
                acquire: (signal: AbortSignal) =>
                  client.acquireAssetDecoration(
                    {
                      snapshot,
                      manifest,
                      assetId,
                      ownerId: `nle-waveform-${runtimeEpoch}-${ordinal}`,
                      runtimeEpoch,
                      derivativeKind: "audio_peaks",
                    },
                    signal,
                  ),
              },
            ];
          });
    source.update(demands);
    // IMPORTANT: don't clear demands in effect cleanup; a timeline revision reruns this effect,
    // and the transient empty set aborts the shared single-worker waveform build.
  }, [
    cache,
    cacheRevision,
    eligibleAssets,
    fingerprints,
    leaseClient,
    manifest,
    runtimeEpoch,
    snapshot,
    source,
    visibleAssetIds.join("\u0000"),
  ]);

  useEffect(
    () => () => {
      source?.close();
      cache.close();
    },
    [cache, source],
  );

  return useMemo(() => {
    const result = new Map<string, AuthoringAudioPeaks>();
    if (source === null || leaseClient === undefined) return result;
    for (const assetId of new Set(visibleAssetIds)) {
      if (!eligibleAssets.has(assetId)) continue;
      const assetFingerprint = fingerprints.get(assetId);
      if (assetFingerprint === undefined) continue;
      const value = cache.get({
        workspaceHandle: snapshot.workspaceHandle,
        assetFingerprint,
        derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
        derivativeKind: "audio_peaks",
      });
      if (value !== undefined) result.set(assetId, value);
    }
    return result;
  }, [
    cache,
    cacheRevision,
    eligibleAssets,
    fingerprints,
    leaseClient,
    snapshot.workspaceHandle,
    source,
    visibleAssetIds.join("\u0000"),
  ]);
}
