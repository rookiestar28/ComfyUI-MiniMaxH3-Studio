import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { disposeAuthoringAudioPeaks } from "../../contracts/authoringAudioPeaks";

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
import type { NleDecorationDemandBroker } from "../../host/nleDecorationDemandBroker";
import { createNleDecorationCache } from "../../host/nleDecorationCache";
import { buildPublicAssetManifest } from "../../runtime/publicAssetManifest";

export type NleFilmstripDecoration = Readonly<{
  bitmap: ImageBitmap;
  width: number;
  height: number;
  sourceWidth: number;
  sourceHeight: number;
  tileCount: number;
}>;

type CachedFilmstrip = NleFilmstripDecoration & Readonly<{ close(): void }>;

export function useNleFilmstrips({
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
}>): ReadonlyMap<string, NleFilmstripDecoration> {
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
  const cache = useMemo(() => createNleDecorationCache<CachedFilmstrip>(), []);
  const [cacheRevision, setCacheRevision] = useState(0);
  const priorAuthority = useRef({
    workspaceHandle: snapshot.workspaceHandle,
    runtimeEpoch,
  });
  const publish = useCallback(
    (value: AuthoringMediaAssetDecoration["value"]) => {
      if (value.derivativeKind !== "filmstrip") {
        if (value.derivativeKind === "audio_peaks")
          disposeAuthoringAudioPeaks(value.peaks);
        else value.bitmap.close();
        return;
      }
      cache.set(value.cacheKey, {
        bitmap: value.bitmap,
        width: value.geometry.derivativeWidth,
        height: value.geometry.derivativeHeight,
        sourceWidth: value.geometry.sourceWidth,
        sourceHeight: value.geometry.sourceHeight,
        tileCount: value.tileCount,
        close: () => value.bitmap.close(),
      });
      setCacheRevision((current) => current + 1);
    },
    [cache],
  );
  const source = useMemo(
    () =>
      decorationDemandBroker?.register(
        "nle-timeline-filmstrips",
        "filmstrip",
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
            if (seen.has(assetId)) return [];
            seen.add(assetId);
            const asset = manifest.assets.find(
              (item) => item.assetId === assetId,
            );
            const assetFingerprint = fingerprints.get(assetId);
            if (
              asset?.kind !== "video" ||
              assetFingerprint === undefined ||
              asset.sourceFrameCount === null ||
              cache.get({
                workspaceHandle: snapshot.workspaceHandle,
                assetFingerprint,
                derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
                derivativeKind: "filmstrip",
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
                      ownerId: `nle-filmstrip-${runtimeEpoch}-${ordinal}`,
                      runtimeEpoch,
                      derivativeKind: "filmstrip",
                    },
                    signal,
                  ),
              },
            ];
          });
    source.update(demands);
    // IMPORTANT: don't clear demands in effect cleanup; a timeline revision reruns this effect,
    // and the transient empty set aborts the shared single-worker filmstrip build.
  }, [
    cache,
    cacheRevision,
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
    const result = new Map<string, NleFilmstripDecoration>();
    if (source === null || leaseClient === undefined) return result;
    for (const assetId of new Set(visibleAssetIds)) {
      const assetFingerprint = fingerprints.get(assetId);
      if (assetFingerprint === undefined) continue;
      const value = cache.get({
        workspaceHandle: snapshot.workspaceHandle,
        assetFingerprint,
        derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
        derivativeKind: "filmstrip",
      });
      if (value !== undefined) result.set(assetId, value);
    }
    return result;
  }, [
    cache,
    cacheRevision,
    fingerprints,
    leaseClient,
    snapshot.workspaceHandle,
    source,
    visibleAssetIds.join("\u0000"),
  ]);
}
