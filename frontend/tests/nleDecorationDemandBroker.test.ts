import { describe, expect, it, vi } from "vitest";

import {
  createNleDecorationDemandBroker,
  type NleRoutedDecorationValue,
} from "../src/host/nleDecorationDemandBroker";
import {
  createNleLeaseScheduler,
  type NleDecorationDemand,
  type NleLeaseScheduler,
} from "../src/host/nleLeaseScheduler";

type Value = Readonly<{ label: string }>;

const demand = (key: string): NleDecorationDemand<Value> => ({
  key,
  acquire: vi.fn(),
});

describe("M25-49/M25-52 decoration demand broker", () => {
  it("runs filmstrip and waveform after playback acquisition while its owner remains retained", async () => {
    const scheduler =
      createNleLeaseScheduler<NleRoutedDecorationValue<Value>>();
    const broker = createNleDecorationDemandBroker(scheduler);
    const published: string[] = [];
    const filmstrips = broker.register("filmstrips", "filmstrip", (value) =>
      published.push(`filmstrip:${value.label}`),
    );
    const waveforms = broker.register("waveforms", "audio_peaks", (value) =>
      published.push(`audio_peaks:${value.label}`),
    );
    const retainedOwner = { retained: true };
    const playbackOwner = await scheduler.acquirePlayback(async () =>
      Promise.resolve(retainedOwner),
    );
    expect(playbackOwner).toBe(retainedOwner);

    const filmstripAcquire = vi.fn(async () => ({
      value: { label: "filmstrip-ready" },
      release: async () => undefined,
    }));
    const waveformAcquire = vi.fn(async () => ({
      value: { label: "waveform-ready" },
      release: async () => undefined,
    }));
    filmstrips.update([{ key: "asset-a", acquire: filmstripAcquire }]);
    waveforms.update([{ key: "asset-a", acquire: waveformAcquire }]);

    try {
      await vi.waitFor(
        () => {
          expect(filmstripAcquire).toHaveBeenCalledOnce();
          expect(waveformAcquire).toHaveBeenCalledOnce();
        },
        { timeout: 100 },
      );
      expect(published).toEqual([
        "filmstrip:filmstrip-ready",
        "audio_peaks:waveform-ready",
      ]);
      expect(retainedOwner.retained).toBe(true);
    } finally {
      broker.close();
      scheduler.close();
    }
  });

  it("orders thumbnails before filmstrips before waveforms", () => {
    const updates: Array<
      readonly NleDecorationDemand<NleRoutedDecorationValue<Value>>[]
    > = [];
    let route: (value: NleRoutedDecorationValue<Value>) => void = () =>
      undefined;
    const scheduler = {
      updateDecorationDemand(next, publish) {
        updates.push(next);
        route = publish;
      },
    } as Pick<
      NleLeaseScheduler<NleRoutedDecorationValue<Value>>,
      "updateDecorationDemand"
    >;
    const thumbnailPublish = vi.fn();
    const filmstripPublish = vi.fn();
    const waveformPublish = vi.fn();
    const broker = createNleDecorationDemandBroker(scheduler);
    const filmstrips = broker.register(
      "filmstrips",
      "filmstrip",
      filmstripPublish,
    );
    const thumbnails = broker.register(
      "thumbnails",
      "thumbnail",
      thumbnailPublish,
    );
    const waveforms = broker.register(
      "waveforms",
      "audio_peaks",
      waveformPublish,
    );

    waveforms.update([demand("peaks-a")]);
    filmstrips.update([demand("film-a")]);
    thumbnails.update([demand("thumb-a"), demand("thumb-b")]);
    expect(updates.at(-1)?.map((item) => item.key)).toEqual([
      "thumbnails\u0000thumb-a",
      "thumbnails\u0000thumb-b",
      "filmstrips\u0000film-a",
      "waveforms\u0000peaks-a",
    ]);

    route({ sourceKey: "filmstrips", value: { label: "paint" } });
    expect(filmstripPublish).toHaveBeenCalledWith({ label: "paint" });
    expect(thumbnailPublish).not.toHaveBeenCalled();

    filmstrips.close();
    expect(updates.at(-1)?.map((item) => item.key)).toEqual([
      "thumbnails\u0000thumb-a",
      "thumbnails\u0000thumb-b",
      "waveforms\u0000peaks-a",
    ]);
    broker.close();
    expect(updates.at(-1)).toEqual([]);
  });

  it("orders a playback preparation after every decoration, whenever it registered", () => {
    const updates: Array<
      readonly NleDecorationDemand<NleRoutedDecorationValue<Value>>[]
    > = [];
    const scheduler = {
      updateDecorationDemand(next) {
        updates.push(next);
      },
    } as Pick<
      NleLeaseScheduler<NleRoutedDecorationValue<Value>>,
      "updateDecorationDemand"
    >;
    const broker = createNleDecorationDemandBroker(scheduler);
    // Registered first and named to sort first: only its priority puts it last.
    const preparation = broker.register(
      "a-preparation",
      "playback_preparation",
      vi.fn(),
    );
    const waveforms = broker.register("waveforms", "audio_peaks", vi.fn());
    const thumbnails = broker.register("thumbnails", "thumbnail", vi.fn());
    const filmstrips = broker.register("filmstrips", "filmstrip", vi.fn());

    preparation.update([demand("asset-a:video"), demand("asset-a:audio")]);
    waveforms.update([demand("peaks-a")]);
    thumbnails.update([demand("thumb-a")]);
    filmstrips.update([demand("film-a")]);

    expect(updates.at(-1)?.map((item) => item.key)).toEqual([
      "thumbnails\u0000thumb-a",
      "filmstrips\u0000film-a",
      "waveforms\u0000peaks-a",
      "a-preparation\u0000asset-a:video",
      "a-preparation\u0000asset-a:audio",
    ]);
    expect(
      updates.at(-1)?.map((item) => (item as { kind?: string }).kind),
    ).toEqual([
      "thumbnail",
      "filmstrip",
      "audio_peaks",
      "playback_preparation",
      "playback_preparation",
    ]);
    broker.close();
  });

  it("publishes each source the member of the value type that its own demands acquired", async () => {
    type Drawn = Readonly<{ label: string }>;
    type Prepared = Readonly<{ prepared: true; asset: string }>;
    const scheduler =
      createNleLeaseScheduler<NleRoutedDecorationValue<Drawn | Prepared>>();
    const broker = createNleDecorationDemandBroker(scheduler);
    const drawn: Drawn[] = [];
    const preparedAssets: string[] = [];
    // Each callback is typed for its own member; neither has to tell the two apart.
    const thumbnails = broker.register<Drawn>(
      "thumbnails",
      "thumbnail",
      (value) => drawn.push(value),
    );
    const preparation = broker.register<Prepared>(
      "preparation",
      "playback_preparation",
      (value) => preparedAssets.push(value.asset),
    );
    preparation.update([
      {
        key: "asset-a",
        acquire: async () => ({
          value: { prepared: true, asset: "asset-a" },
          release: async () => undefined,
        }),
      },
    ]);
    thumbnails.update([
      {
        key: "asset-a",
        acquire: async () => ({
          value: { label: "thumb" },
          release: async () => undefined,
        }),
      },
    ]);
    try {
      await vi.waitFor(() => expect(preparedAssets).toEqual(["asset-a"]), {
        timeout: 500,
      });
      expect(drawn).toEqual([{ label: "thumb" }]);
    } finally {
      broker.close();
      scheduler.close();
    }
  });

  it("refuses a priority it does not know", () => {
    const scheduler = {
      updateDecorationDemand: vi.fn(),
    } as unknown as Pick<
      NleLeaseScheduler<NleRoutedDecorationValue<Value>>,
      "updateDecorationDemand"
    >;
    const broker = createNleDecorationDemandBroker(scheduler);
    expect(() =>
      broker.register("unknown", "playback" as never, vi.fn()),
    ).toThrow(/invalid/u);
    broker.close();
  });

  it("does not requeue published keys until their producer withdraws and re-adds them", async () => {
    const scheduler =
      createNleLeaseScheduler<NleRoutedDecorationValue<Value>>();
    const broker = createNleDecorationDemandBroker(scheduler);
    const release = vi.fn(async () => undefined);
    const makeAcquire = (label: string) =>
      vi.fn(async () => ({
        value: { label },
        release,
      }));
    const filmstripAcquire = makeAcquire("filmstrip");
    const waveformAcquire = makeAcquire("waveform");
    const thumbnailAcquire = makeAcquire("thumbnail");
    const filmstrips = broker.register("filmstrips", "filmstrip", vi.fn());
    const waveforms = broker.register("waveforms", "audio_peaks", vi.fn());
    const thumbnails = broker.register("thumbnails", "thumbnail", vi.fn());
    const filmstrip = { key: "asset-a", acquire: filmstripAcquire };
    filmstrips.update([filmstrip]);
    waveforms.update([{ key: "asset-a", acquire: waveformAcquire }]);
    try {
      await scheduler.whenIdle();
      expect(filmstripAcquire).toHaveBeenCalledOnce();
      expect(waveformAcquire).toHaveBeenCalledOnce();

      thumbnails.update([{ key: "asset-b", acquire: thumbnailAcquire }]);
      await scheduler.whenIdle();
      expect(thumbnailAcquire).toHaveBeenCalledOnce();
      expect(filmstripAcquire).toHaveBeenCalledOnce();
      expect(waveformAcquire).toHaveBeenCalledOnce();

      const freshAuthority = makeAcquire("fresh filmstrip");
      filmstrips.update([{ key: "asset-a", acquire: freshAuthority }]);
      await scheduler.whenIdle();
      expect(freshAuthority).not.toHaveBeenCalled();
      filmstrips.update([]);
      filmstrips.update([{ key: "asset-a", acquire: freshAuthority }]);
      await scheduler.whenIdle();
      expect(freshAuthority).toHaveBeenCalledOnce();
      expect(release).toHaveBeenCalledTimes(4);
    } finally {
      broker.close();
      scheduler.close();
    }
  });

  it("refuses duplicate producer keys and ignores stale source handles", () => {
    const scheduler = {
      updateDecorationDemand: vi.fn(),
    } as unknown as Pick<
      NleLeaseScheduler<NleRoutedDecorationValue<Value>>,
      "updateDecorationDemand"
    >;
    const broker = createNleDecorationDemandBroker(scheduler);
    const first = broker.register("filmstrips", "filmstrip", vi.fn());
    expect(() => broker.register("filmstrips", "thumbnail", vi.fn())).toThrow();
    first.close();
    first.update([demand("stale")]);
    expect(scheduler.updateDecorationDemand).toHaveBeenLastCalledWith(
      [],
      expect.any(Function),
    );
  });

  it("does not restart an in-flight demand when another producer stays empty", () => {
    const scheduler = {
      updateDecorationDemand: vi.fn(),
    } as unknown as Pick<
      NleLeaseScheduler<NleRoutedDecorationValue<Value>>,
      "updateDecorationDemand"
    >;
    const broker = createNleDecorationDemandBroker(scheduler);
    const thumbnails = broker.register("thumbnails", "thumbnail", vi.fn());
    const thumbnail = demand("thumb-a");
    thumbnails.update([thumbnail]);
    const callsWithThumbnailActive = vi.mocked(scheduler.updateDecorationDemand)
      .mock.calls.length;

    const filmstrips = broker.register("filmstrips", "filmstrip", vi.fn());
    filmstrips.update([]);

    expect(scheduler.updateDecorationDemand).toHaveBeenCalledTimes(
      callsWithThumbnailActive,
    );
  });

  it("refreshes same-key acquisition authority without restarting its active route", async () => {
    const scheduler =
      createNleLeaseScheduler<NleRoutedDecorationValue<Value>>();
    const broker = createNleDecorationDemandBroker(scheduler);
    const published: Value[] = [];
    const timeline = broker.register("timeline", "filmstrip", (value) =>
      published.push(value),
    );
    let rejectOld!: (error: unknown) => void;
    let oldSignal: AbortSignal | undefined;
    const oldAcquire = vi.fn((signal: AbortSignal) => {
      oldSignal = signal;
      return new Promise<{ value: Value; release(): Promise<void> }>(
        (_resolve, reject) => {
          rejectOld = reject;
        },
      );
    });
    const currentAcquire = vi.fn(async () => ({
      value: { label: "current" },
      release: async () => undefined,
    }));

    timeline.update([
      { key: "workspace:asset-fingerprint", acquire: oldAcquire },
    ]);
    await vi.waitFor(() => expect(oldAcquire).toHaveBeenCalledOnce());
    timeline.update([
      { key: "workspace:asset-fingerprint", acquire: currentAcquire },
    ]);

    expect(oldSignal?.aborted).toBe(false);
    rejectOld(
      Object.assign(new Error("snapshot revision changed"), {
        disposition: "stale",
      }),
    );
    try {
      await scheduler.whenIdle();
      expect(oldAcquire).toHaveBeenCalledOnce();
      expect(currentAcquire).toHaveBeenCalledOnce();
      expect(oldSignal?.aborted).toBe(false);
      expect(published).toEqual([{ label: "current" }]);
    } finally {
      broker.close();
      scheduler.close();
    }
  });

  it("reschedules a same-key demand when fresh authority arrives after stale work has ended", async () => {
    const scheduler =
      createNleLeaseScheduler<NleRoutedDecorationValue<Value>>();
    const broker = createNleDecorationDemandBroker(scheduler);
    const published: Value[] = [];
    const timeline = broker.register("timeline", "filmstrip", (value) =>
      published.push(value),
    );
    const stale = Object.assign(new Error("snapshot revision changed"), {
      disposition: "stale",
    });
    const oldAcquire = vi.fn(async () => {
      throw stale;
    });
    const currentAcquire = vi.fn(async () => ({
      value: { label: "current-authority" },
      release: async () => undefined,
    }));

    timeline.update([{ key: "workspace:asset-a", acquire: oldAcquire }]);
    await scheduler.whenIdle();
    expect(oldAcquire).toHaveBeenCalledOnce();
    expect(scheduler.snapshot().activeKey).toBeNull();

    timeline.update([{ key: "workspace:asset-a", acquire: currentAcquire }]);
    try {
      await vi.waitFor(() => expect(currentAcquire).toHaveBeenCalledOnce(), {
        timeout: 100,
      });
      await scheduler.whenIdle();
      expect(published).toEqual([{ label: "current-authority" }]);
    } finally {
      broker.close();
      scheduler.close();
    }
  });
});
