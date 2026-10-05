import { useEffect, useMemo, useRef, useState } from "react";

import { canonicalPublicRuntimeAssetFingerprint } from "../../contracts/authoringMediaLeaseCodec";
import type { NleAuthoringStateV2 } from "../../contracts/authoringWorkbenchCodec";
import type {
  AuthoringMediaDecorationLeaseClient,
  AuthoringMediaPreparedPlayback,
  AuthoringMediaSourceLeaseClient,
} from "../../host/authoringMediaSourceLease";
import type { NleDecorationDemandBroker } from "../../host/nleDecorationDemandBroker";
import type { NleDecorationDemand } from "../../host/nleLeaseScheduler";
import { buildNleAuthoringAssetManifest } from "../../runtime/nleAuthoringAssetManifest";

type Prepared = AuthoringMediaPreparedPlayback["value"];
type PreparationKind = Prepared["derivativeKind"];

/** How often one asset and kind may be refused for want of capacity before it is given up. */
export const NLE_ASSET_PREPARATION_CAPACITY_OFFERS = 3;

const preparationKey = (
  workspaceHandle: string,
  assetFingerprint: string,
  kind: PreparationKind,
): string => `${workspaceHandle}:${assetFingerprint}:${kind}`;

const dispositionOf = (error: unknown): string | null =>
  typeof error === "object" && error !== null && "disposition" in error
    ? String((error as { disposition: unknown }).disposition)
    : null;

/**
 * Asks the service to generate the playback derivatives of every video asset in the catalog, so
 * that the first clip of an asset finds them already generated. It holds nothing in the page:
 * each preparation is one lease, created and released, queued behind every decoration.
 */
export function useNleAssetPreparation({
  authoring,
  runtimeEpoch,
  leaseClient,
  decorationDemandBroker,
}: Readonly<{
  authoring: NleAuthoringStateV2 | undefined;
  runtimeEpoch: number;
  leaseClient: AuthoringMediaSourceLeaseClient | undefined;
  decorationDemandBroker: NleDecorationDemandBroker<Prepared> | undefined;
}>): void {
  const manifest = useMemo(
    () =>
      authoring === undefined
        ? undefined
        : buildNleAuthoringAssetManifest(authoring),
    [authoring],
  );
  // What is known about each asset and kind. `ended` is for good: prepared, or refused for a
  // reason that asking again cannot change. `waiting` names the authoring state a key was last
  // refused under and is not offered under again. `yielding` holds the keys that ask nothing
  // at their next acquisition: those aborted or completed since the list was last offered.
  const ledger = useRef({
    ended: new Set<string>(),
    waiting: new Map<string, string>(),
    capacityRefusals: new Map<string, number>(),
    yielding: new Set<string>(),
  }).current;
  const [ledgerRevision, setLedgerRevision] = useState(0);
  const source = useMemo(
    () =>
      decorationDemandBroker?.register<Prepared>(
        "nle-asset-preparation",
        "playback_preparation",
        (value) => {
          ledger.ended.add(
            preparationKey(
              value.cacheKey.workspaceHandle,
              value.cacheKey.assetFingerprint,
              value.derivativeKind,
            ),
          );
          setLedgerRevision((current) => current + 1);
        },
      ) ?? null,
    [decorationDemandBroker, ledger],
  );

  useEffect(() => {
    if (source === null) return;
    const client =
      leaseClient !== undefined &&
      "prepareAssetPlayback" in leaseClient &&
      typeof leaseClient.prepareAssetPlayback === "function"
        ? (leaseClient as AuthoringMediaDecorationLeaseClient)
        : null;
    if (authoring === undefined || manifest === undefined || client === null) {
      source.update([]);
      return;
    }
    const authoringFingerprint = authoring.authoringFingerprint;
    const offered = new Set<string>();
    const demands = manifest.assets.flatMap((asset, ordinal) => {
      if (
        asset.kind !== "video" ||
        asset.sourceFrameCount === null ||
        asset.landmarks.length !== asset.sourceFrameCount
      )
        return [];
      const assetFingerprint = canonicalPublicRuntimeAssetFingerprint(asset);
      const kinds: readonly PreparationKind[] =
        asset.embeddedAudio === "present_bound" &&
        asset.sourceSampleCount !== null
          ? ["video_proxy", "audio_preview"]
          : ["video_proxy"];
      return kinds.flatMap((kind): NleDecorationDemand<Prepared>[] => {
        const key = preparationKey(
          authoring.workspaceHandle,
          assetFingerprint,
          kind,
        );
        // Catalog identifiers are unique, so this never drops a real asset. An entry
        // listed twice must not reach the broker, which refuses duplicate keys.
        if (offered.has(key)) return [];
        offered.add(key);
        if (
          ledger.ended.has(key) ||
          ledger.waiting.get(key) === authoringFingerprint
        )
          return [];
        return [
          {
            key,
            kind: "playback_preparation",
            async acquire(signal: AbortSignal) {
              // IMPORTANT: this list is as old as the render that offered it, and the scheduler
              // asks for its demands after the ledger has moved on. It puts a demand that
              // playback preempted back at the head of its queue, ahead of every decoration
              // offered meanwhile; and another source's update re-queues what this source last
              // listed, before the next render has withdrawn a key. Only an offer of this list
              // puts a preparation back in the broker's priority order, and one made when the
              // abort arrives would be answered before the scheduler has put the demand back,
              // while one made from a later render can come after playback has its source. So
              // an acquisition of a key that is not due asks the service nothing: it ends at
              // once, which takes the demand off the queue, and has the list offered again.
              // Without it a preparation, up to a whole encode, would run before the filmstrip
              // of the clip the user has just placed, and a refused key would be asked again
              // under the state that refused it. A mark is not removed here: playback arriving
              // in this very instant puts the demand at the head once more.
              if (
                ledger.yielding.has(key) ||
                ledger.ended.has(key) ||
                ledger.waiting.get(key) === authoringFingerprint
              ) {
                setLedgerRevision((current) => current + 1);
                throw new Error("asset preparation is not due");
              }
              try {
                const prepared = await client.prepareAssetPlayback!(
                  {
                    authoring,
                    manifest,
                    assetId: asset.assetId,
                    ownerId: `nle-preparation-${runtimeEpoch}-${ordinal}`,
                    runtimeEpoch,
                    derivativeKind: kind,
                  },
                  signal,
                );
                return {
                  value: prepared.value,
                  async release() {
                    try {
                      await prepared.release();
                    } catch (error) {
                      // The body was generated; only its lease could not be ended, and the
                      // scheduler publishes nothing after a failed release. Without this the
                      // key would be offered again and each offer would take another lease.
                      ledger.ended.add(key);
                      setLedgerRevision((current) => current + 1);
                      throw error;
                    }
                    // The scheduler publishes next, and the key is then never offered again.
                    // If playback has arrived, at any moment up to that one, it publishes
                    // nothing and puts the demand back as it does for an aborted one.
                    ledger.yielding.add(key);
                  },
                };
              } catch (error) {
                // Aborted by playback or by a changed demand list: the scheduler put the
                // demand back, or the list no longer has it. Nothing was learnt about the key.
                if (signal.aborted) {
                  ledger.yielding.add(key);
                  throw error;
                }
                const disposition = dispositionOf(error);
                if (disposition === "stale") {
                  // The state this request names has been superseded. It is offered again under
                  // the next one; the broker rebinds this very acquisition if that one is here.
                  ledger.waiting.set(key, authoringFingerprint);
                  setLedgerRevision((current) => current + 1);
                  throw error;
                }
                if (
                  disposition === "busy" ||
                  disposition === "resource_limit"
                ) {
                  const refusals = (ledger.capacityRefusals.get(key) ?? 0) + 1;
                  ledger.capacityRefusals.set(key, refusals);
                  if (refusals >= NLE_ASSET_PREPARATION_CAPACITY_OFFERS)
                    ledger.ended.add(key);
                  else ledger.waiting.set(key, authoringFingerprint);
                  setLedgerRevision((current) => current + 1);
                  // IMPORTANT: the scheduler answers these two dispositions by holding its whole
                  // queue for a back-off and then keeping the refused demand at its head. That
                  // is right for a thumbnail the user is waiting for; for a preparation it
                  // would stall every decoration behind work that only saves time later. The
                  // refusal is therefore absorbed here and the error carries no disposition.
                  throw new Error("asset preparation deferred");
                }
                ledger.ended.add(key);
                setLedgerRevision((current) => current + 1);
                throw error;
              }
            },
          },
        ];
      });
    });
    for (const key of [...ledger.ended])
      if (!offered.has(key)) ledger.ended.delete(key);
    for (const key of [...ledger.waiting.keys()])
      if (!offered.has(key)) ledger.waiting.delete(key);
    for (const key of [...ledger.capacityRefusals.keys()])
      if (!offered.has(key)) ledger.capacityRefusals.delete(key);
    // This offer has the scheduler rebuild its queue in priority order, so no preparation is
    // ahead of a decoration any more and none has to give way. A mark that stayed would make
    // the key give way at every acquisition and never be prepared.
    ledger.yielding.clear();
    source.update(demands);
    // IMPORTANT: do not clear the demands in an effect cleanup. An accepted edit reruns this
    // effect, and a transient empty list would abort a preparation that is still generating.
  }, [
    authoring,
    ledger,
    ledgerRevision,
    leaseClient,
    manifest,
    runtimeEpoch,
    source,
  ]);

  useEffect(() => () => source?.close(), [source]);
}
