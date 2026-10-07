import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  NLE_ASSET_PREPARATION_CAPACITY_OFFERS,
  useNleAssetPreparation,
} from "../src/components/nle/useNleAssetPreparation";
import { canonicalPublicRuntimeAssetFingerprint } from "../src/contracts/authoringMediaLeaseCodec";
import type { NleAuthoringStateV2 } from "../src/contracts/authoringWorkbenchCodec";
import type {
  AuthoringMediaDecorationLeaseClient,
  AuthoringMediaPreparedPlayback,
  NleAuthoringMediaAssetPreparationContext,
} from "../src/host/authoringMediaSourceLease";
import {
  createNleDecorationDemandBroker,
  type NleDecorationDemandBroker,
  type NleRoutedDecorationValue,
} from "../src/host/nleDecorationDemandBroker";
import {
  createNleLeaseScheduler,
  type NleLeaseScheduler,
  type NleLeaseSchedulerDependencies,
} from "../src/host/nleLeaseScheduler";
import {
  PREPARATION_ASSETS,
  PREPARATION_ASSETS_NO_DECODER_MAKES,
  preparationAuthoring,
} from "./support/nleAssetPreparationFixture";

afterEach(cleanup);

type Prepared = AuthoringMediaPreparedPlayback["value"];
type Thumbnail = Readonly<{ derivativeKind: "thumbnail"; label: string }>;
type Prepare = NonNullable<
  AuthoringMediaDecorationLeaseClient["prepareAssetPlayback"]
>;

const assetNamed = (assetId: string) =>
  [...PREPARATION_ASSETS, ...PREPARATION_ASSETS_NO_DECODER_MAKES].find(
    (asset) => asset.assetId === assetId,
  )!;
const keyOf = (assetId: string, kind: Prepared["derivativeKind"]) =>
  `workspace-preparation:${canonicalPublicRuntimeAssetFingerprint(
    assetNamed(assetId),
  )}:${kind}`;

const refused = (disposition: string) =>
  Object.assign(new Error(disposition), { disposition });

/** What the lease client answers for a preparation that succeeded. */
const prepared = (
  context: NleAuthoringMediaAssetPreparationContext,
  release: () => Promise<void> = async () => undefined,
): AuthoringMediaPreparedPlayback => ({
  value: {
    derivativeKind: context.derivativeKind,
    cacheKey: {
      workspaceHandle: context.authoring.workspaceHandle,
      assetFingerprint: canonicalPublicRuntimeAssetFingerprint(
        context.manifest.assets.find(
          ({ assetId }) => assetId === context.assetId,
        )!,
      ),
      derivativeProfileId: "h3.authoring.media_derivatives.v6",
      derivativeKind: context.derivativeKind,
    },
  },
  release,
});

type Scheduler = NleLeaseScheduler<
  NleRoutedDecorationValue<Prepared | Thumbnail>
>;

const PREPARATION_ROUTE = `nle-asset-preparation\u0000${keyOf(
  "vid-silent",
  "video_proxy",
)}`;
const THUMBNAIL_ROUTE = "thumbnails\u0000thumbnail-a";

/** A lease client whose first preparation runs until it is aborted; later ones succeed. */
const abortableOnce =
  (order: string[]): Prepare =>
  (context, signal) => {
    order.push(`prepare:${context.assetId}`);
    if (order.filter((entry) => entry.startsWith("prepare:")).length > 1)
      return Promise.resolve(prepared(context));
    return new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(refused("cancelled")), {
        once: true,
      });
    });
  };

/** A decoration offered through the same broker; it records when it is acquired. */
function offerThumbnail(
  broker: NleDecorationDemandBroker<Prepared | Thumbnail>,
  order: string[],
): void {
  // Like the bin: once its thumbnail is published it stops asking for it.
  const source = broker.register<Thumbnail>("thumbnails", "thumbnail", () =>
    source.update([]),
  );
  source.update([
    {
      key: "thumbnail-a",
      acquire: async () => {
        order.push("thumbnail");
        return {
          value: { derivativeKind: "thumbnail", label: "a" },
          release: async () => undefined,
        };
      },
    },
  ]);
}

/** The product's own scheduler and broker under the hook; only the lease client is a double. */
function workspace(
  prepare: Prepare,
  options: Readonly<{
    assets?: Parameters<typeof preparationAuthoring>[1];
    dependencies?: Partial<NleLeaseSchedulerDependencies>;
    /** Runs before the hook mounts, while the scheduler has seen no demand. */
    before?(scheduler: Scheduler): void;
  }> = {},
) {
  const scheduler: Scheduler = createNleLeaseScheduler(options.dependencies);
  const broker = createNleDecorationDemandBroker(scheduler);
  options.before?.(scheduler);
  const prepareAssetPlayback = vi.fn(prepare);
  const leaseClient = {
    prepareAssetPlayback,
  } as unknown as AuthoringMediaDecorationLeaseClient;
  const state = (revision: number) =>
    preparationAuthoring(revision, options.assets);
  const hook = renderHook(
    ({ authoring }: { authoring: NleAuthoringStateV2 }) =>
      useNleAssetPreparation({
        authoring,
        runtimeEpoch: 7,
        leaseClient,
        decorationDemandBroker: broker,
      }),
    { initialProps: { authoring: state(1) } },
  );
  const asked = () =>
    prepareAssetPlayback.mock.calls.map(
      ([context]) => `${context.assetId}:${context.derivativeKind}`,
    );
  return {
    scheduler,
    broker,
    prepareAssetPlayback,
    hook,
    state,
    asked,
    accept(revision: number) {
      hook.rerender({ authoring: state(revision) });
    },
    close() {
      hook.unmount();
      broker.close();
      scheduler.close();
    },
  };
}

describe("asset playback preparation demand", () => {
  it("offers each video asset and kind once, in catalog order, as the lowest priority", async () => {
    const update = vi.fn();
    const close = vi.fn();
    const register = vi.fn(() => ({ update, close }));
    const broker = {
      register,
      close: vi.fn(),
    } as unknown as NleDecorationDemandBroker<Prepared>;
    const prepareAssetPlayback = vi.fn();
    const leaseClient = {
      prepareAssetPlayback,
    } as unknown as AuthoringMediaDecorationLeaseClient;
    // An entry listed twice is one key; the broker refuses duplicate keys from one source.
    const authoring = preparationAuthoring(1, [
      ...PREPARATION_ASSETS,
      assetNamed("vid-silent"),
      ...PREPARATION_ASSETS_NO_DECODER_MAKES,
    ]);

    const hook = renderHook(() =>
      useNleAssetPreparation({
        authoring,
        runtimeEpoch: 7,
        leaseClient,
        decorationDemandBroker: broker,
      }),
    );

    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(register).toHaveBeenCalledExactlyOnceWith(
      "nle-asset-preparation",
      "playback_preparation",
      expect.any(Function),
    );
    const demands = update.mock.calls.at(-1)![0] as readonly Readonly<{
      key: string;
      acquire(signal: AbortSignal): Promise<unknown>;
    }>[];
    expect(demands.map(({ key }) => key)).toEqual([
      keyOf("vid-audio", "video_proxy"),
      keyOf("vid-audio", "audio_preview"),
      keyOf("vid-silent", "video_proxy"),
      // Bound audio without a sample count: the picture is prepared, the audio is not asked for.
      keyOf("vid-uncounted", "video_proxy"),
      // What the decoder never makes. An image is not prepared though it is timed like a video
      // (`img-timed` gives no key), and audio that is not bound is not asked for whatever
      // sample count stands beside it.
      keyOf("vid-unbound", "video_proxy"),
    ]);
    const signal = new AbortController().signal;
    void demands[1]!.acquire(signal).catch(() => undefined);
    expect(prepareAssetPlayback).toHaveBeenCalledExactlyOnceWith(
      {
        authoring,
        manifest: expect.objectContaining({
          authoringFingerprint: authoring.authoringFingerprint,
        }),
        assetId: "vid-audio",
        ownerId: "nle-preparation-7-1",
        runtimeEpoch: 7,
        derivativeKind: "audio_preview",
      },
      signal,
    );
    hook.unmount();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("stops listing a key that is prepared, given up, or refused under the present state", async () => {
    type Listed = readonly Readonly<{
      key: string;
      acquire(
        signal: AbortSignal,
      ): Promise<Readonly<{ value: Prepared; release(): Promise<void> }>>;
    }>[];
    const update = vi.fn((_demands: Listed) => undefined);
    const register = vi.fn(
      (
        _sourceKey: string,
        _priority: string,
        _publish: (value: Prepared) => void,
      ) => ({ update, close: vi.fn() }),
    );
    const broker = {
      register,
      close: vi.fn(),
    } as unknown as NleDecorationDemandBroker<Prepared>;
    const refusals: Readonly<Record<string, string>> = {
      "vid-silent": "unsupported",
      "vid-uncounted": "busy",
    };
    const leaseClient = {
      prepareAssetPlayback: (
        context: NleAuthoringMediaAssetPreparationContext,
      ) => {
        const disposition = refusals[context.assetId];
        return disposition === undefined
          ? Promise.resolve(prepared(context))
          : Promise.reject(refused(disposition));
      },
    } as unknown as AuthoringMediaDecorationLeaseClient;
    const hook = renderHook(
      ({ authoring }: { authoring: NleAuthoringStateV2 }) =>
        useNleAssetPreparation({
          authoring,
          runtimeEpoch: 7,
          leaseClient,
          decorationDemandBroker: broker,
        }),
      { initialProps: { authoring: preparationAuthoring(1) } },
    );
    const list = () => update.mock.calls.at(-1)![0];
    const listed = () => list().map(({ key }) => key);
    const demand = (key: string) =>
      list().find((candidate) => candidate.key === key)!;
    const publish = register.mock.calls[0]![2];
    const signal = new AbortController().signal;
    const [picture, sound, silent, uncounted] = [
      keyOf("vid-audio", "video_proxy"),
      keyOf("vid-audio", "audio_preview"),
      keyOf("vid-silent", "video_proxy"),
      keyOf("vid-uncounted", "video_proxy"),
    ] as const;
    expect(listed()).toEqual([picture, sound, silent, uncounted]);

    // Prepared: acquired, released and published, as the scheduler does it.
    const lease = await demand(picture).acquire(signal);
    await lease.release();
    act(() => publish(lease.value));
    expect(listed()).toEqual([sound, silent, uncounted]);

    // Given up.
    await act(async () => {
      await expect(demand(silent).acquire(signal)).rejects.toMatchObject({
        disposition: "unsupported",
      });
    });
    expect(listed()).toEqual([sound, uncounted]);

    // Refused for want of capacity: not listed under this state, listed again under the next.
    await act(async () => {
      await expect(demand(uncounted).acquire(signal)).rejects.toThrow(
        "asset preparation deferred",
      );
    });
    expect(listed()).toEqual([sound]);
    hook.rerender({ authoring: preparationAuthoring(2) });
    expect(listed()).toEqual([sound, uncounted]);
    hook.unmount();
  });

  it.each([
    ["no lease client", undefined, preparationAuthoring()],
    [
      "a lease client that cannot prepare",
      {
        acquireAssetDecoration: vi.fn(),
      } as unknown as AuthoringMediaDecorationLeaseClient,
      preparationAuthoring(),
    ],
    [
      "no authoring state",
      {
        prepareAssetPlayback: vi.fn(),
      } as unknown as AuthoringMediaDecorationLeaseClient,
      undefined,
    ],
  ] as const)(
    "offers nothing with %s",
    async (_name, leaseClient, authoring) => {
      const update = vi.fn();
      const broker = {
        register: vi.fn(() => ({ update, close: vi.fn() })),
        close: vi.fn(),
      } as unknown as NleDecorationDemandBroker<Prepared>;
      renderHook(() =>
        useNleAssetPreparation({
          authoring,
          runtimeEpoch: 7,
          leaseClient,
          decorationDemandBroker: broker,
        }),
      );
      await waitFor(() => expect(update).toHaveBeenCalled());
      expect(update.mock.calls.every(([demands]) => demands.length === 0)).toBe(
        true,
      );
    },
  );

  it("prepares each asset and kind once, releases it, and never asks again", async () => {
    const releases: string[] = [];
    const rig = workspace(async (context) =>
      prepared(context, async () => {
        releases.push(`${context.assetId}:${context.derivativeKind}`);
      }),
    );
    const everything = [
      "vid-audio:video_proxy",
      "vid-audio:audio_preview",
      "vid-silent:video_proxy",
      "vid-uncounted:video_proxy",
    ];
    try {
      await waitFor(() => expect(rig.asked()).toEqual(everything));
      await rig.scheduler.whenIdle();
      expect(releases).toEqual(everything);

      // Accepted edits change the authoring state; what is prepared stays prepared.
      rig.accept(2);
      rig.accept(3);
      await rig.scheduler.whenIdle();
      await waitFor(() =>
        expect(rig.scheduler.snapshot().pendingKeys).toEqual([]),
      );
      expect(rig.asked()).toEqual(everything);
    } finally {
      rig.close();
    }
  });

  it("waits behind a decoration that is offered at the same time", async () => {
    const order: string[] = [];
    let finishPlayback!: () => void;
    let playback!: Promise<void>;
    const rig = workspace(
      async (context) => {
        order.push(`prepare:${context.assetId}`);
        return prepared(context);
      },
      {
        assets: [assetNamed("vid-silent")],
        // Playback holds the scheduler while both are offered, so neither has started.
        before(scheduler) {
          playback = scheduler.acquirePlayback(
            () =>
              new Promise<void>((resolve) => {
                finishPlayback = resolve;
              }),
          );
        },
      },
    );
    try {
      offerThumbnail(rig.broker, order);
      expect(rig.scheduler.snapshot().pendingKeys).toEqual([
        THUMBNAIL_ROUTE,
        PREPARATION_ROUTE,
      ]);
      expect(order).toEqual([]);
      finishPlayback();
      await playback;
      await waitFor(() =>
        expect(order).toEqual(["thumbnail", "prepare:vid-silent"]),
      );
    } finally {
      rig.close();
    }
  });

  it("goes back behind the decorations offered while playback preempted it", async () => {
    const order: string[] = [];
    const rig = workspace(abortableOnce(order), {
      assets: [assetNamed("vid-silent")],
    });
    try {
      await waitFor(() => expect(order).toEqual(["prepare:vid-silent"]));
      let finishPlayback!: () => void;
      const playback = rig.scheduler.acquirePlayback(
        () =>
          new Promise<void>((resolve) => {
            finishPlayback = resolve;
          }),
      );
      offerThumbnail(rig.broker, order);
      // The scheduler's own rule: the preempted preparation is back at the head of the queue,
      // ahead of the thumbnail, for as long as playback is acquiring.
      await waitFor(() =>
        expect(rig.scheduler.snapshot().pendingKeys).toEqual([
          PREPARATION_ROUTE,
          THUMBNAIL_ROUTE,
        ]),
      );
      finishPlayback();
      await playback;
      // At the head it asks the service nothing, and the thumbnail goes first.
      await waitFor(() =>
        expect(order).toEqual([
          "prepare:vid-silent",
          "thumbnail",
          "prepare:vid-silent",
        ]),
      );
      await rig.scheduler.whenIdle();
      expect(rig.asked()).toHaveLength(2);
    } finally {
      rig.close();
    }
  });

  it("still gives way when playback arrives in the instant it gives way", async () => {
    const order: string[] = [];
    let scheduler!: Scheduler;
    let starts = 0;
    let second: Promise<void> | undefined;
    const rig = workspace(abortableOnce(order), {
      assets: [assetNamed("vid-silent")],
      before(created) {
        scheduler = created;
      },
      dependencies: {
        onDiagnostic(event) {
          if (
            event.event !== "decoration.acquire.start" ||
            event.activeKind !== "playback_preparation"
          )
            return;
          starts += 1;
          // The second start is the acquisition that gives way. Playback arrives before it has
          // ended, so the scheduler takes it for a preempted one and puts it back at the head.
          if (starts === 2)
            second = scheduler.acquirePlayback(async () => undefined);
        },
      },
    });
    try {
      await waitFor(() => expect(order).toEqual(["prepare:vid-silent"]));
      let finishPlayback!: () => void;
      const playback = rig.scheduler.acquirePlayback(
        () =>
          new Promise<void>((resolve) => {
            finishPlayback = resolve;
          }),
      );
      offerThumbnail(rig.broker, order);
      await waitFor(() =>
        expect(rig.scheduler.snapshot().pendingKeys).toEqual([
          PREPARATION_ROUTE,
          THUMBNAIL_ROUTE,
        ]),
      );
      finishPlayback();
      await playback;
      await waitFor(() =>
        expect(order).toEqual([
          "prepare:vid-silent",
          "thumbnail",
          "prepare:vid-silent",
        ]),
      );
      await second;
      await rig.scheduler.whenIdle();
      // Asked twice, started more often: at the head of the queue it gave way each time.
      expect(rig.asked()).toHaveLength(2);
      expect(starts).toBeGreaterThanOrEqual(4);
    } finally {
      rig.close();
    }
  });

  it("gives way after a completion that playback kept from being published", async () => {
    const order: string[] = [];
    let endRelease: (() => void) | undefined;
    const rig = workspace(
      async (context) => {
        order.push(`prepare:${context.assetId}`);
        return order.length > 1
          ? prepared(context)
          : prepared(
              context,
              () =>
                new Promise<void>((resolve) => {
                  endRelease = resolve;
                }),
            );
      },
      { assets: [assetNamed("vid-silent")] },
    );
    try {
      // The preparation has completed and its lease is being ended.
      await waitFor(() => expect(endRelease).toBeDefined());
      let finishPlayback!: () => void;
      const playback = rig.scheduler.acquirePlayback(
        () =>
          new Promise<void>((resolve) => {
            finishPlayback = resolve;
          }),
      );
      offerThumbnail(rig.broker, order);
      endRelease!();
      // Nothing was published, and the scheduler put the demand back at the head.
      await waitFor(() =>
        expect(rig.scheduler.snapshot().pendingKeys).toEqual([
          PREPARATION_ROUTE,
          THUMBNAIL_ROUTE,
        ]),
      );
      finishPlayback();
      await playback;
      await waitFor(() =>
        expect(order).toEqual([
          "prepare:vid-silent",
          "thumbnail",
          "prepare:vid-silent",
        ]),
      );
      await rig.scheduler.whenIdle();
      expect(rig.asked()).toHaveLength(2);
    } finally {
      rig.close();
    }
  });

  it("gives way to playback and is asked again once playback has its source", async () => {
    const signals: AbortSignal[] = [];
    const rig = workspace(
      (context, signal) => {
        signals.push(signal);
        if (signals.length > 1) return Promise.resolve(prepared(context));
        return new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(refused("cancelled")), {
            once: true,
          });
        });
      },
      { assets: [assetNamed("vid-silent")] },
    );
    try {
      await waitFor(() => expect(signals).toHaveLength(1));
      const order: string[] = [];
      const owner = await rig.scheduler.acquirePlayback(async () => {
        // Playback's own acquisition starts only after the preparation has let go.
        order.push(signals[0]!.aborted ? "after-abort" : "before-abort");
        return "playback-owner";
      });
      expect(owner).toBe("playback-owner");
      expect(order).toEqual(["after-abort"]);

      await waitFor(() => expect(signals).toHaveLength(2));
      expect(signals[1]!.aborted).toBe(false);
      await rig.scheduler.whenIdle();
      // Prepared on the second attempt; a later edit asks nothing more.
      rig.accept(2);
      await rig.scheduler.whenIdle();
      expect(rig.asked()).toEqual([
        "vid-silent:video_proxy",
        "vid-silent:video_proxy",
      ]);
    } finally {
      rig.close();
    }
  });

  it.each(["busy", "resource_limit"] as const)(
    "does not hold the queue for a %s refusal and offers the asset again only under a new state, three times at most",
    async (disposition) => {
      const schedule = vi.fn((callback: () => void, delayMs: number) =>
        globalThis.setTimeout(callback, delayMs),
      );
      const rig = workspace(async () => Promise.reject(refused(disposition)), {
        assets: [assetNamed("vid-silent")],
        dependencies: { schedule },
      });
      const thumbnailRan = vi.fn();
      try {
        await waitFor(() => expect(rig.asked()).toHaveLength(1));
        await rig.scheduler.whenIdle();
        // No back-off was scheduled, and the scheduler is not waiting on the refused demand.
        expect(schedule).not.toHaveBeenCalled();
        expect(rig.scheduler.snapshot()).toMatchObject({
          activeKey: null,
          idleAfterBackoff: false,
        });
        await waitFor(() =>
          expect(rig.scheduler.snapshot().pendingKeys).toEqual([]),
        );

        // A decoration offered next runs at once.
        const thumbnails = rig.broker.register<Thumbnail>(
          "thumbnails",
          "thumbnail",
          () => undefined,
        );
        thumbnails.update([
          {
            key: "thumbnail-a",
            acquire: async () => {
              thumbnailRan();
              return {
                value: { derivativeKind: "thumbnail", label: "a" },
                release: async () => undefined,
              };
            },
          },
        ]);
        await waitFor(() => expect(thumbnailRan).toHaveBeenCalledOnce());
        await rig.scheduler.whenIdle();
        // The other source's update did not bring the refused preparation back.
        expect(rig.asked()).toHaveLength(1);

        // The same state again: still nothing. A new state: one more offer, up to the bound.
        rig.accept(1);
        await rig.scheduler.whenIdle();
        expect(rig.asked()).toHaveLength(1);
        for (
          let offer = 2;
          offer <= NLE_ASSET_PREPARATION_CAPACITY_OFFERS;
          offer += 1
        ) {
          rig.accept(offer);
          await waitFor(() => expect(rig.asked()).toHaveLength(offer));
          await rig.scheduler.whenIdle();
        }
        expect(NLE_ASSET_PREPARATION_CAPACITY_OFFERS).toBe(3);
        rig.accept(NLE_ASSET_PREPARATION_CAPACITY_OFFERS + 1);
        rig.accept(NLE_ASSET_PREPARATION_CAPACITY_OFFERS + 2);
        await rig.scheduler.whenIdle();
        await waitFor(() =>
          expect(rig.scheduler.snapshot().pendingKeys).toEqual([]),
        );
        expect(rig.asked()).toHaveLength(NLE_ASSET_PREPARATION_CAPACITY_OFFERS);
        expect(schedule).not.toHaveBeenCalled();
      } finally {
        rig.close();
      }
    },
  );

  it.each([
    ["a refusal that ends the key", "unsupported", [11]],
    ["a capacity refusal", "busy", [11, 12, 13]],
  ] as const)(
    "asks nothing more under the same state after %s, though another source's update re-queues the demand",
    async (_name, disposition, revisionsAsked) => {
      const order: string[] = [];
      let broker: NleDecorationDemandBroker<Prepared | Thumbnail> | undefined;
      let settled = 0;
      const rig = workspace(async () => Promise.reject(refused(disposition)), {
        assets: [assetNamed("vid-silent")],
        dependencies: {
          onDiagnostic(event) {
            // The preparation has just been refused and the hook has not rendered since. Another
            // source's update makes the broker hand the scheduler every list again, this
            // source's as it was last offered, so the refused demand is queued once more.
            if (event.event === "decoration.settled" && settled++ === 0)
              offerThumbnail(broker!, order);
          },
        },
      });
      broker = rig.broker;
      try {
        await waitFor(() => expect(order).toEqual(["thumbnail"]));
        await rig.scheduler.whenIdle();
        await waitFor(() =>
          expect(rig.scheduler.snapshot().pendingKeys).toEqual([]),
        );
        expect(rig.asked()).toHaveLength(1);

        for (const revision of [2, 3, 4]) {
          rig.accept(revision);
          await rig.scheduler.whenIdle();
          await waitFor(() =>
            expect(rig.scheduler.snapshot().pendingKeys).toEqual([]),
          );
        }
        // One request for each state that may ask, and none twice under one state.
        expect(
          rig.prepareAssetPlayback.mock.calls.map(
            ([context]) => context.authoring.timelineRevision,
          ),
        ).toEqual(revisionsAsked);
      } finally {
        rig.close();
      }
    },
  );

  it("asks a stale preparation again under the state that replaced it", async () => {
    const rig = workspace(
      async (context) => {
        if (context.authoring.timelineRevision === 11) throw refused("stale");
        return prepared(context);
      },
      { assets: [assetNamed("vid-silent")] },
    );
    try {
      await waitFor(() => expect(rig.asked()).toHaveLength(1));
      await rig.scheduler.whenIdle();
      // The state it was refused under is not asked again.
      rig.accept(1);
      await rig.scheduler.whenIdle();
      expect(rig.asked()).toHaveLength(1);

      rig.accept(2);
      await waitFor(() => expect(rig.asked()).toHaveLength(2));
      expect(
        rig.prepareAssetPlayback.mock.calls[1]![0].authoring.timelineRevision,
      ).toBe(12);
      await rig.scheduler.whenIdle();
      rig.accept(3);
      await rig.scheduler.whenIdle();
      expect(rig.asked()).toHaveLength(2);
    } finally {
      rig.close();
    }
  });

  it("rebinds a preparation that turns stale while it runs, without aborting it", async () => {
    const signals: AbortSignal[] = [];
    let rejectFirst!: (error: unknown) => void;
    const rig = workspace(
      (context, signal) => {
        signals.push(signal);
        if (signals.length > 1) return Promise.resolve(prepared(context));
        return new Promise((_resolve, reject) => {
          rejectFirst = reject;
        });
      },
      { assets: [assetNamed("vid-silent")] },
    );
    try {
      await waitFor(() => expect(signals).toHaveLength(1));
      rig.accept(2);
      expect(signals[0]!.aborted).toBe(false);
      expect(signals).toHaveLength(1);

      rejectFirst(refused("stale"));
      await waitFor(() => expect(signals).toHaveLength(2));
      expect(signals[0]!.aborted).toBe(false);
      expect(
        rig.prepareAssetPlayback.mock.calls[1]![0].authoring.timelineRevision,
      ).toBe(12);
      await rig.scheduler.whenIdle();
      rig.accept(3);
      await rig.scheduler.whenIdle();
      expect(signals).toHaveLength(2);
    } finally {
      rig.close();
    }
  });

  it.each(["unsupported", "generation_failed", "contract_mismatch"] as const)(
    "gives an asset up after a %s refusal",
    async (disposition) => {
      const rig = workspace(async () => Promise.reject(refused(disposition)), {
        assets: [assetNamed("vid-silent")],
      });
      try {
        await waitFor(() => expect(rig.asked()).toHaveLength(1));
        await rig.scheduler.whenIdle();
        rig.accept(2);
        rig.accept(3);
        await rig.scheduler.whenIdle();
        await waitFor(() =>
          expect(rig.scheduler.snapshot().pendingKeys).toEqual([]),
        );
        expect(rig.asked()).toHaveLength(1);
      } finally {
        rig.close();
      }
    },
  );

  it("does not ask again for a body whose lease could not be ended", async () => {
    const rig = workspace(
      async (context) =>
        prepared(context, async () => {
          throw refused("internal_failure");
        }),
      { assets: [assetNamed("vid-silent")] },
    );
    try {
      await waitFor(() => expect(rig.asked()).toHaveLength(1));
      await rig.scheduler.whenIdle();
      rig.accept(2);
      rig.accept(3);
      await rig.scheduler.whenIdle();
      await waitFor(() =>
        expect(rig.scheduler.snapshot().pendingKeys).toEqual([]),
      );
      expect(rig.asked()).toHaveLength(1);
    } finally {
      rig.close();
    }
  });

  it("withdraws a running preparation when the workspace closes", async () => {
    const signals: AbortSignal[] = [];
    const rig = workspace(
      (_context, signal) => {
        signals.push(signal);
        return new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(refused("cancelled")), {
            once: true,
          });
        });
      },
      { assets: [assetNamed("vid-silent")] },
    );
    await waitFor(() => expect(signals).toHaveLength(1));
    rig.hook.unmount();
    expect(signals[0]!.aborted).toBe(true);
    await rig.scheduler.whenIdle();
    expect(signals).toHaveLength(1);
    rig.broker.close();
    rig.scheduler.close();
  });
});
