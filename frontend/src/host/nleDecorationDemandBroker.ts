import type {
  NleDecorationDemand,
  NleLeaseScheduler,
} from "./nleLeaseScheduler";

export type NleDecorationDemandPriority =
  "thumbnail" | "filmstrip" | "audio_peaks" | "playback_preparation";

export type NleRoutedDecorationValue<T> = Readonly<{
  sourceKey: string;
  value: T;
}>;

type SchedulerPort<T> = Pick<
  NleLeaseScheduler<NleRoutedDecorationValue<T>>,
  "updateDecorationDemand"
>;

type Source<T> = {
  priority: NleDecorationDemandPriority;
  demands: readonly NleDecorationDemand<T>[];
  publish(value: T): void;
  generation: number;
};

type DemandRoute<T> = Readonly<{
  sourceKey: string;
  kind: NleDecorationDemandPriority;
  demand: NleDecorationDemand<T>;
}>;

type RoutedDemand<T> = NleDecorationDemand<NleRoutedDecorationValue<T>> &
  Readonly<{ sourceKey: string; kind: NleDecorationDemandPriority }>;

export type NleDecorationDemandSource<T> = Readonly<{
  update(demands: readonly NleDecorationDemand<T>[]): void;
  close(): void;
}>;

export type NleDecorationDemandBroker<T> = Readonly<{
  /**
   * `V` is the member of the broker's value type that this source acquires and is published.
   * A source that names none gets the whole type.
   */
  register<V extends T = T>(
    sourceKey: string,
    priority: NleDecorationDemandPriority,
    publishValue: (value: V) => void,
  ): NleDecorationDemandSource<V>;
  close(): void;
}>;

const priorities: Readonly<Record<NleDecorationDemandPriority, number>> = {
  thumbnail: 0,
  filmstrip: 1,
  audio_peaks: 2,
  // IMPORTANT: preparation is last. It only saves a later generation, so every decoration that
  // is waiting to be drawn goes before it; ranked above one, a queue of preparations would hold
  // back thumbnails the user is looking at.
  playback_preparation: 3,
};

const isStale = (error: unknown): boolean =>
  typeof error === "object" &&
  error !== null &&
  "disposition" in error &&
  (error as { disposition: unknown }).disposition === "stale";

export const createNleDecorationDemandBroker = <T>(
  scheduler: SchedulerPort<T>,
): NleDecorationDemandBroker<T> => {
  const sources = new Map<string, Source<T>>();
  const routedDemands = new Map<string, RoutedDemand<T>>();
  const fulfilledKeys = new Set<string>();
  const publicationKeys = new WeakMap<NleRoutedDecorationValue<T>, string>();
  let closed = false;
  let currentRoutes = new Map<string, DemandRoute<T>>();
  let lastRoutes: readonly RoutedDemand<T>[] | null = null;
  let lastAuthorities: readonly DemandRoute<T>[] | null = null;

  const publish = (routed: NleRoutedDecorationValue<T>): void => {
    if (closed) return;
    const key = publicationKeys.get(routed);
    // IMPORTANT: release precedes publication, but React cache effects may run later.
    // Other producers must not requeue a published key before its source withdraws it.
    if (key !== undefined) fulfilledKeys.add(key);
    sources.get(routed.sourceKey)?.publish(routed.value);
  };

  const flush = (): void => {
    const registeredRoutes = [...sources]
      .sort(
        ([keyA, sourceA], [keyB, sourceB]) =>
          priorities[sourceA.priority] - priorities[sourceB.priority] ||
          keyA.localeCompare(keyB),
      )
      .flatMap(([sourceKey, source]) =>
        source.demands.map((demand) => ({
          sourceKey,
          kind: source.priority,
          demand,
        })),
      );
    const registeredKeys = new Set(
      registeredRoutes.map(
        ({ sourceKey, demand }) => `${sourceKey}\u0000${demand.key}`,
      ),
    );
    // A withdrawn key may be requested again after cache eviction or owner replacement.
    for (const key of fulfilledKeys)
      if (!registeredKeys.has(key)) fulfilledKeys.delete(key);
    const nextRoutes = registeredRoutes.filter(
      ({ sourceKey, demand }) =>
        !fulfilledKeys.has(`${sourceKey}\u0000${demand.key}`),
    );
    const nextCurrentRoutes = new Map<string, DemandRoute<T>>();
    const routes = nextRoutes.map(({ sourceKey, kind, demand }) => {
      const key = `${sourceKey}\u0000${demand.key}`;
      nextCurrentRoutes.set(key, { sourceKey, kind, demand });
      let routed = routedDemands.get(key);
      if (routed === undefined) {
        routed = Object.freeze({
          key,
          sourceKey,
          kind,
          async acquire(signal: AbortSignal) {
            let route = currentRoutes.get(key);
            while (route !== undefined) {
              try {
                const lease = await route.demand.acquire(signal);
                const value = Object.freeze({
                  sourceKey: route.sourceKey,
                  value: lease.value,
                });
                publicationKeys.set(value, key);
                return Object.freeze({
                  value,
                  release: () => lease.release(),
                  ...(lease.discard === undefined
                    ? {}
                    : { discard: () => lease.discard?.() }),
                });
              } catch (error) {
                const replacement = currentRoutes.get(key);
                // IMPORTANT: timeline edits change the lease revision but not the source key.
                // Rebind only a stale in-flight request to the latest snapshot instead of
                // aborting/restarting the shared derivative worker on every accepted edit.
                if (
                  signal.aborted ||
                  !isStale(error) ||
                  replacement === undefined ||
                  replacement.demand === route.demand
                )
                  throw error;
                route = replacement;
              }
            }
            throw new Error("decoration demand is no longer registered");
          },
        });
        routedDemands.set(key, routed);
      }
      return routed;
    });
    currentRoutes = nextCurrentRoutes;
    for (const key of routedDemands.keys())
      if (!nextCurrentRoutes.has(key)) routedDemands.delete(key);
    // IMPORTANT: React recreates acquisition closures when timeline revisions change. Keep each
    // routed wrapper stable while refreshing currentRoutes; the scheduler can preserve active
    // work and requeue the same key if an earlier stale acquisition already settled.
    if (
      lastRoutes !== null &&
      lastAuthorities !== null &&
      routes.length === lastRoutes.length &&
      routes.every((route, index) => route === lastRoutes![index]) &&
      nextRoutes.length === lastAuthorities.length &&
      nextRoutes.every((route, index) => {
        const previous = lastAuthorities![index]!;
        return (
          route.sourceKey === previous.sourceKey &&
          route.kind === previous.kind &&
          route.demand === previous.demand
        );
      })
    )
      return;
    lastRoutes = routes;
    lastAuthorities = nextRoutes;
    scheduler.updateDecorationDemand(routes, publish);
  };

  return Object.freeze({
    register<V extends T = T>(
      sourceKey: string,
      priority: NleDecorationDemandPriority,
      publishValue: (value: V) => void,
    ) {
      if (
        closed ||
        !sourceKey ||
        sourceKey.includes("\u0000") ||
        sources.has(sourceKey) ||
        !(priority in priorities)
      )
        throw new Error("decoration demand source is invalid");
      const source: Source<T> = {
        priority,
        demands: [],
        // A value is routed by the source key of the demand that acquired it, so this source is
        // only ever published what its own demands returned: its `V`, whatever else `T` holds.
        publish: publishValue as (value: T) => void,
        generation: 1,
      };
      sources.set(sourceKey, source);
      flush();
      const generation = source.generation;
      let sourceClosed = false;
      return Object.freeze({
        update(demands: readonly NleDecorationDemand<V>[]): void {
          if (sourceClosed || closed || sources.get(sourceKey) !== source)
            return;
          if (
            new Set(demands.map((demand) => demand.key)).size !== demands.length
          )
            throw new Error("decoration demand keys must be unique per source");
          source.demands = [...demands];
          flush();
        },
        close(): void {
          if (sourceClosed) return;
          sourceClosed = true;
          if (sources.get(sourceKey)?.generation === generation) {
            sources.delete(sourceKey);
            if (!closed) flush();
          }
        },
      });
    },
    close(): void {
      if (closed) return;
      closed = true;
      sources.clear();
      currentRoutes = new Map();
      routedDemands.clear();
      fulfilledKeys.clear();
      scheduler.updateDecorationDemand([], publish);
    },
  });
};
