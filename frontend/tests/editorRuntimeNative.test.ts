import { afterEach, describe, expect, it, vi } from "vitest";

import {
  NATIVE_MEDIA_OPERATION_DEADLINE_MS,
  createHtmlMediaElementTransportFactory,
  type HtmlMediaElementSourceOwner,
  type MediaPresentation,
  type MediaTransportOpen,
} from "../src/runtime/editorRuntime";
import type { PublicRuntimeAsset } from "../src/runtime/publicAssetManifest";

const asset: PublicRuntimeAsset = Object.freeze({
  assetId: "asset-video",
  kind: "video",
  sourceTimeBase: Object.freeze({ num: 1, den: 12_288 }),
  sourceFrameCount: 48,
  sourceSampleCount: 96_000,
  embeddedAudio: "present_bound",
  timestampPolicy: "nonnegative_monotonic_v1",
  landmarks: Object.freeze([
    Object.freeze({ frameIndex: 0, pts: 0, dts: -1_024, durationTicks: 512 }),
    Object.freeze({
      frameIndex: 12,
      pts: 6_144,
      dts: 5_120,
      durationTicks: 512,
    }),
  ]),
});

type FrameCallback = (
  now: DOMHighResTimeStamp,
  metadata: VideoFrameCallbackMetadata,
) => void;

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

function nativeHarness(
  options: {
    rvfc?: boolean;
    play?: () => Promise<void>;
    microsecondFloor?: boolean;
  } = {},
) {
  const listeners = new Map<string, Set<EventListenerOrEventListenerObject>>();
  const callbacks = new Map<number, FrameCallback>();
  let callbackSequence = 0;
  let currentTime = 0;
  let paused = true;
  let seeking = false;
  const play = vi.fn(() => {
    const result = options.play?.() ?? Promise.resolve();
    void result.then(
      () => {
        paused = false;
      },
      () => undefined,
    );
    return result;
  });
  const pause = vi.fn(() => {
    paused = true;
  });
  const removeAttribute = vi.fn();
  const load = vi.fn();
  const cancelVideoFrameCallback = vi.fn((id: number) => callbacks.delete(id));
  const requestVideoFrameCallback = vi.fn((callback: FrameCallback) => {
    const id = ++callbackSequence;
    callbacks.set(id, callback);
    return id;
  });
  const video = {
    get currentTime() {
      return currentTime;
    },
    set currentTime(value: number) {
      if (!options.microsecondFloor) {
        currentTime = value;
        return;
      }
      const assigned = Math.floor(value * 1_000_000) / 1_000_000;
      currentTime = Math.floor(assigned * 1_000_000) / 1_000_000;
    },
    get paused() {
      return paused;
    },
    get seeking() {
      return seeking;
    },
    get duration() {
      return 2;
    },
    play,
    pause,
    removeAttribute,
    load,
    addEventListener(
      type: string,
      listener: EventListenerOrEventListenerObject,
    ) {
      const values = listeners.get(type) ?? new Set();
      values.add(listener);
      listeners.set(type, values);
    },
    removeEventListener(
      type: string,
      listener: EventListenerOrEventListenerObject,
    ) {
      listeners.get(type)?.delete(listener);
    },
    requestVideoFrameCallback:
      options.rvfc === false ? undefined : requestVideoFrameCallback,
    cancelVideoFrameCallback:
      options.rvfc === false ? undefined : cancelVideoFrameCallback,
  } as unknown as HTMLVideoElement;

  const dispatch = (type: string) => {
    const event = new Event(type);
    for (const listener of [...(listeners.get(type) ?? [])]) {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    }
  };

  return {
    video,
    play,
    pause,
    removeAttribute,
    load,
    requestVideoFrameCallback,
    cancelVideoFrameCallback,
    dispatch,
    present(mediaTime: number) {
      for (const [id, callback] of [...callbacks]) {
        callbacks.delete(id);
        callback(0, { mediaTime } as VideoFrameCallbackMetadata);
      }
    },
    setCurrentTime(value: number) {
      currentTime = value;
    },
    setSeeking(value: boolean) {
      seeking = value;
    },
    pendingCallbacks: () => callbacks.size,
    listenerCount: (type: string) => listeners.get(type)?.size ?? 0,
  };
}

function request(
  ownerId = "owner-a",
  signal = new AbortController().signal,
): MediaTransportOpen {
  return { asset, ownerId, epoch: 1, signal };
}

describe("native decoder reauthorization", () => {
  it("preserves its source and lifetime identity while refusing changed, aborted and closed owners", async () => {
    const native = nativeHarness();
    const rebind = vi.fn(async () => true);
    const release = vi.fn(async () => undefined);
    const factory = createHtmlMediaElementTransportFactory(async () => ({
      element: native.video,
      rebind,
      release,
    }));
    const first = request();
    const transport = await factory(first);
    const next = { ...first, epoch: 2, signal: new AbortController().signal };
    expect(await transport.rebind!(next)).toBe(true);
    expect(rebind).toHaveBeenCalledExactlyOnceWith(next);
    expect(transport.openedEpoch).toBe(1);
    expect(native.load).not.toHaveBeenCalled();
    expect(native.removeAttribute).not.toHaveBeenCalled();
    expect(release).not.toHaveBeenCalled();
    expect(await transport.rebind!({ ...next, ownerId: "other-owner" })).toBe(
      false,
    );
    expect(
      await transport.rebind!({
        ...next,
        asset: { ...asset, sourceSampleCount: 97_000 },
      }),
    ).toBe(false);
    const abort = new AbortController();
    abort.abort();
    expect(await transport.rebind!({ ...next, signal: abort.signal })).toBe(
      false,
    );
    expect(rebind).toHaveBeenCalledOnce();
    await transport.close();
    expect(await transport.rebind!(next)).toBe(false);
    expect(release).toHaveBeenCalledOnce();
  });
});

function seekRequest(epoch = 1, signal = new AbortController().signal) {
  return { epoch, sourceFrame: 12, sourcePts: 6_144, signal };
}

async function openTransport(
  native: ReturnType<typeof nativeHarness>,
  release = vi.fn(async () => undefined),
  observer:
    | "request_video_frame_callback"
    | "event_fallback" = "request_video_frame_callback",
  seekObservation:
    "observed_interval" | "exact_source_pts" = "observed_interval",
) {
  const factory = createHtmlMediaElementTransportFactory(
    async () => ({ element: native.video, release }),
    { frameObserver: observer, seekObservation },
  );
  return { transport: await factory(request()), release };
}

async function openExactTransport(
  native: ReturnType<typeof nativeHarness>,
  release = vi.fn(async () => undefined),
) {
  return openTransport(
    native,
    release,
    "request_video_frame_callback",
    "exact_source_pts",
  );
}

afterEach(() => {
  vi.useRealTimers();
});

describe("M25-12 native HTML media transport", () => {
  it.each(["NotAllowedError", "NotSupportedError"])(
    "observes only a bounded live native playback failure for %s",
    async (name) => {
      const native = nativeHarness({
        play: () =>
          Promise.reject(new DOMException("synthetic private detail", name)),
      });
      const observation = vi.fn(() => {
        throw new Error("observer isolation");
      });
      const factory = createHtmlMediaElementTransportFactory(
        async () => ({ element: native.video, release: async () => undefined }),
        { onPlaybackFailure: observation },
      );
      const transport = await factory(request());
      const seeking = transport.seek(seekRequest());
      native.present(0.5);
      await seeking;
      await expect(
        transport.play(new AbortController().signal),
      ).rejects.toMatchObject({ code: "transport_failure" });
      expect(observation).toHaveBeenCalledExactlyOnceWith({
        ownerId: "owner-a",
        openedEpoch: 1,
        reason:
          name === "NotAllowedError"
            ? "autoplay_blocked"
            : "playback_unavailable",
      });
      await transport.close();
    },
  );

  it("does not report a late native denial after cancellation as a current failure", async () => {
    const pending = deferred<void>();
    const native = nativeHarness({ play: () => pending.promise });
    const observation = vi.fn();
    const factory = createHtmlMediaElementTransportFactory(
      async () => ({ element: native.video, release: async () => undefined }),
      { onPlaybackFailure: observation },
    );
    const transport = await factory(request());
    const seeking = transport.seek(seekRequest());
    native.present(0.5);
    await seeking;
    const abort = new AbortController();
    const playing = transport.play(abort.signal);
    abort.abort();
    await expect(playing).rejects.toMatchObject({ code: "cancelled" });
    pending.reject(new DOMException("late", "NotAllowedError"));
    await Promise.resolve();
    await Promise.resolve();
    expect(observation).not.toHaveBeenCalled();
    await transport.close();
  });

  it("keeps the requested VFR anchor while reporting RVFC mediaTime as observed PTS", async () => {
    const native = nativeHarness();
    const { transport } = await openTransport(native);
    const observed: MediaPresentation[] = [];
    transport.subscribe((value) => observed.push(value));

    const seeking = transport.seek(seekRequest());
    expect(transport.pendingFrameObservations).toBe(1);
    native.present(0.625);
    await expect(seeking).resolves.toMatchObject({
      epoch: 1,
      sourceFrame: 12,
      sourcePts: 6_144,
      observedSourcePts: 7_680,
      observation: "request_video_frame_callback",
    });

    const callbackRegistrations =
      native.requestVideoFrameCallback.mock.calls.length;
    await expect(transport.seek(seekRequest(2))).resolves.toMatchObject({
      epoch: 2,
      sourceFrame: 12,
      sourcePts: 6_144,
      observedSourcePts: 7_680,
    });
    expect(native.requestVideoFrameCallback).toHaveBeenCalledTimes(
      callbackRegistrations,
    );

    await transport.play(new AbortController().signal);
    expect(transport.pendingFrameObservations).toBe(1);
    native.setCurrentTime(0.75);
    native.present(0.75);
    expect(observed.at(-1)).toMatchObject({
      epoch: 2,
      sourceFrame: 12,
      sourcePts: 6_144,
      observedSourcePts: 9_216,
      observation: "request_video_frame_callback",
    });
    expect(transport.pendingFrameObservations).toBe(1);
    await transport.pause();
    expect(transport.pendingFrameObservations).toBe(0);
    await transport.close();
  });

  it("uses the selected event fallback even when RVFC exists", async () => {
    const native = nativeHarness();
    const { transport } = await openTransport(
      native,
      vi.fn(async () => undefined),
      "event_fallback",
    );
    const seeking = transport.seek(seekRequest());
    native.setCurrentTime(0.5);
    native.dispatch("seeked");
    await expect(seeking).resolves.toMatchObject({
      sourceFrame: 12,
      sourcePts: 6_144,
      observedSourcePts: 6_144,
      observation: "event_fallback",
    });
    expect(native.requestVideoFrameCallback).not.toHaveBeenCalled();
    await transport.close();
  });

  it("rejects a queued pre-seek RVFC and waits for the post-seek presentation", async () => {
    const native = nativeHarness();
    const { transport } = await openTransport(native);
    const seeking = transport.seek(seekRequest());
    native.setSeeking(true);
    native.present(0);
    expect(native.requestVideoFrameCallback).toHaveBeenCalledTimes(2);
    expect(transport.pendingFrameObservations).toBe(1);

    native.setSeeking(false);
    native.present(0.5);
    await expect(seeking).resolves.toMatchObject({
      sourcePts: 6_144,
      observedSourcePts: 6_144,
    });
    await transport.close();
  });

  it("rejects a stale post-seek RVFC until the target presentation arrives", async () => {
    const native = nativeHarness();
    const { transport } = await openExactTransport(native);
    const initial = transport.seek({
      epoch: 1,
      sourceFrame: 0,
      sourcePts: 0,
      signal: new AbortController().signal,
    });
    native.present(0);
    await initial;

    const seeking = transport.seek(seekRequest(2));
    native.setSeeking(false);
    native.dispatch("seeked");
    native.present(0);
    expect(native.pendingCallbacks()).toBe(1);

    native.present(0.5);
    await expect(seeking).resolves.toMatchObject({
      epoch: 2,
      sourceFrame: 12,
      sourcePts: 6_144,
      observedSourcePts: 6_144,
    });
    await transport.close();
  });

  it("rejects an exact-seek RVFC that quantizes to the adjacent source PTS", async () => {
    const native = nativeHarness();
    const { transport } = await openExactTransport(native);
    const seeking = transport.seek(seekRequest());
    native.setSeeking(false);
    native.dispatch("seeked");

    // Exactly half a source tick is inside the floating-point seek window, but half-up
    // quantization assigns it to the next PTS. Exact composition paint must wait for 6144.
    native.present(0.5 + 1 / 24_576);
    expect(native.pendingCallbacks()).toBe(1);

    native.present(0.5);
    await expect(seeking).resolves.toMatchObject({
      sourcePts: 6_144,
      observedSourcePts: 6_144,
    });
    await transport.close();
  });

  it("does not reuse a stale playback observation after a cancelled same-target seek", async () => {
    const native = nativeHarness();
    const { transport } = await openExactTransport(native);
    const atHead = (epoch: number, signal = new AbortController().signal) => ({
      epoch,
      sourceFrame: 0,
      sourcePts: 0,
      signal,
    });
    const initial = transport.seek(atHead(1));
    native.present(0);
    await initial;

    await transport.play(new AbortController().signal);
    native.setCurrentTime(1 / 24);
    native.present(1 / 24);
    await transport.pause();

    // The cancelled seek has already assigned currentTime=0, but it cannot replace the playback
    // observation (PTS 512) without a target RVFC. A successor at the same requested PTS must not
    // mistake that stale observation for a cache hit.
    const abort = new AbortController();
    const cancelled = transport.seek(atHead(2, abort.signal));
    abort.abort();
    await expect(cancelled).rejects.toMatchObject({ code: "cancelled" });

    const successor = transport.seek(atHead(3));
    expect(native.pendingCallbacks()).toBe(1);
    native.present(0);
    await expect(successor).resolves.toMatchObject({
      sourcePts: 0,
      observedSourcePts: 0,
    });
    await transport.close();
  });

  it("quantizes native seek upward without changing the exact PTS target", async () => {
    const native = nativeHarness({ microsecondFloor: true });
    const { transport } = await openExactTransport(native);
    const seek = async (
      epoch: number,
      sourceFrame: number,
      sourcePts: number,
    ) => {
      const abort = new AbortController();
      const pending = transport.seek({
        epoch,
        sourceFrame,
        sourcePts,
        signal: abort.signal,
      });
      const targetSeconds = sourcePts / 12_288;
      try {
        expect(native.video.currentTime).toBeGreaterThanOrEqual(targetSeconds);
        expect(native.video.currentTime - targetSeconds).toBeLessThanOrEqual(
          1 / 12_288 / 2,
        );
        native.present(targetSeconds);
        await expect(pending).resolves.toMatchObject({
          epoch,
          sourceFrame,
          sourcePts,
          observedSourcePts: sourcePts,
          observation: "request_video_frame_callback",
        });
      } finally {
        abort.abort();
        await pending.catch(() => undefined);
      }
    };

    try {
      const sourceFrameCount = asset.sourceFrameCount ?? 0;
      expect(sourceFrameCount).toBe(48);
      for (let sourceFrame = 0; sourceFrame < sourceFrameCount; sourceFrame++) {
        await seek(sourceFrame + 1, sourceFrame, sourceFrame * 512);
        if (sourceFrame === 0 || sourceFrame === 3)
          expect(native.video.currentTime).toBe(sourceFrame / 24);
      }
    } finally {
      await transport.close();
    }
  });

  it.each([
    { sourceTimeBase: { num: 1, den: 4_000_000 }, sourcePts: 1 },
    { sourceTimeBase: { num: 1, den: 1 }, sourcePts: Number.MAX_SAFE_INTEGER },
  ])(
    "rejects native microsecond precision outside the source tick: %j",
    async ({ sourceTimeBase, sourcePts }) => {
      const native = nativeHarness({ microsecondFloor: true });
      const factory = createHtmlMediaElementTransportFactory(
        async () => ({
          element: native.video,
          release: async () => undefined,
        }),
        { seekObservation: "exact_source_pts" },
      );
      const transport = await factory({
        asset: { ...asset, sourceTimeBase },
        ownerId: "owner-a",
        epoch: 1,
        signal: new AbortController().signal,
      });
      await expect(
        transport.seek({
          epoch: 1,
          sourceFrame: 1,
          sourcePts,
          signal: new AbortController().signal,
        }),
      ).rejects.toMatchObject({ code: "capability_mismatch" });
      await transport.close();
    },
  );

  it.each(["seeked", "timeupdate"])(
    "retains an early target RVFC until native %s completes the seek",
    async (event) => {
      const native = nativeHarness();
      const { transport } = await openExactTransport(native);
      const published: MediaPresentation[] = [];
      transport.subscribe((value) => published.push(value));
      const pending = transport.seek(seekRequest());
      native.setSeeking(true);
      native.present(0.5);
      native.dispatch(event);
      expect(published).toEqual([]);
      native.setSeeking(false);
      native.dispatch(event);
      await expect(pending).resolves.toMatchObject({
        observedSourcePts: 6_144,
        observation: "request_video_frame_callback",
      });
      expect(native.pendingCallbacks()).toBe(0);
      expect(transport.pendingFrameObservations).toBe(0);
      await transport.close();
      expect(native.listenerCount(event)).toBe(0);
    },
  );

  it("retains an early target RVFC across a later stale compositor callback", async () => {
    const native = nativeHarness();
    const { transport } = await openExactTransport(native);
    const pending = transport.seek(seekRequest());
    native.setSeeking(true);
    native.present(0.5);

    native.setSeeking(false);
    native.present(0);
    native.dispatch("seeked");
    expect(native.pendingCallbacks()).toBe(0);
    await expect(pending).resolves.toMatchObject({
      sourcePts: 6_144,
      observedSourcePts: 6_144,
    });
    await transport.close();
  });

  it.each([null, 0, 0.5 + 1 / 12_288])(
    "never creates target RVFC evidence from seek events after observation %s",
    async (mediaTime) => {
      vi.useFakeTimers();
      const native = nativeHarness();
      const { transport } = await openExactTransport(native);
      const pending = transport.seek(seekRequest());
      const rejected = expect(pending).rejects.toMatchObject({
        code: "transport_failure",
      });
      native.setSeeking(true);
      if (mediaTime !== null) native.present(mediaTime);
      native.setSeeking(false);
      native.dispatch("seeked");
      await vi.advanceTimersByTimeAsync(NATIVE_MEDIA_OPERATION_DEADLINE_MS);
      await rejected;
      expect(native.pendingCallbacks()).toBe(0);
      await transport.close();
    },
  );

  it("discards an early target observation on cancellation before a successor seek", async () => {
    const native = nativeHarness();
    const { transport } = await openExactTransport(native);
    const abort = new AbortController();
    const first = transport.seek(seekRequest(1, abort.signal));
    const rejected = expect(first).rejects.toMatchObject({ code: "cancelled" });
    native.setSeeking(true);
    native.present(0.5);
    abort.abort();
    await rejected;
    expect(native.pendingCallbacks()).toBe(0);
    const second = transport.seek(seekRequest(2));
    const published: MediaPresentation[] = [];
    transport.subscribe((value) => published.push(value));
    native.setSeeking(false);
    native.dispatch("seeked");
    expect(published).toEqual([]);
    native.present(0.5);
    await expect(second).resolves.toMatchObject({
      epoch: 2,
      observedSourcePts: 6_144,
    });
    await transport.close();
  });

  it.each(["error", "deadline"])(
    "clears early target RVFC evidence after %s",
    async (failure) => {
      vi.useFakeTimers();
      const native = nativeHarness();
      const { transport } = await openExactTransport(native);
      const published: MediaPresentation[] = [];
      transport.subscribe((value) => published.push(value));
      const pending = transport.seek(seekRequest());
      const rejected = expect(pending).rejects.toMatchObject({
        code: "transport_failure",
      });
      native.setSeeking(true);
      native.present(0.5);
      if (failure === "error") native.dispatch("error");
      else
        await vi.advanceTimersByTimeAsync(NATIVE_MEDIA_OPERATION_DEADLINE_MS);
      await rejected;
      native.setSeeking(false);
      native.dispatch("seeked");
      expect(published).toEqual([]);
      expect(native.pendingCallbacks()).toBe(0);
      await transport.close();
    },
  );

  it("does not publish retained evidence when native completion is at another target", async () => {
    vi.useFakeTimers();
    const native = nativeHarness();
    const { transport } = await openExactTransport(native);
    const pending = transport.seek(seekRequest());
    const rejected = expect(pending).rejects.toMatchObject({
      code: "transport_failure",
    });
    native.setSeeking(true);
    native.present(0.5);
    native.setCurrentTime(0.75);
    native.setSeeking(false);
    native.dispatch("seeked");
    await vi.advanceTimersByTimeAsync(NATIVE_MEDIA_OPERATION_DEADLINE_MS);
    await rejected;
    await transport.close();
  });

  it("rejects an observation beyond the finite native media duration", async () => {
    const native = nativeHarness();
    const { transport } = await openTransport(native);
    const beyondDuration = transport.seek({
      epoch: 1,
      sourceFrame: 60,
      sourcePts: 30_720,
      signal: new AbortController().signal,
    });
    const result = expect(beyondDuration).rejects.toMatchObject({
      code: "contract_mismatch",
    });
    native.present(2.5);
    await result;
    await transport.close();
  });

  it("fails capability admission and cleans the decoder when selected RVFC is absent", async () => {
    const native = nativeHarness({ rvfc: false });
    const release = vi.fn(async () => undefined);
    const factory = createHtmlMediaElementTransportFactory(async () => ({
      element: native.video,
      release,
    }));
    await expect(factory(request())).rejects.toMatchObject({
      code: "capability_mismatch",
    });
    expect(native.pause).toHaveBeenCalledTimes(1);
    expect(native.removeAttribute).toHaveBeenCalledWith("src");
    expect(native.load).toHaveBeenCalledTimes(1);
    expect(release).toHaveBeenCalledTimes(1);
  });

  it("cancels a pending seek promptly and bounds a stalled seek", async () => {
    vi.useFakeTimers();
    const native = nativeHarness();
    const { transport } = await openTransport(native);
    const abort = new AbortController();
    const cancelled = transport.seek(seekRequest(1, abort.signal));
    expect(transport.pendingFrameObservations).toBe(1);
    abort.abort();
    await expect(cancelled).rejects.toMatchObject({ code: "cancelled" });
    expect(transport.pendingFrameObservations).toBe(0);
    expect(native.cancelVideoFrameCallback).toHaveBeenCalledTimes(1);

    const stalled = transport.seek(seekRequest(2));
    const stalledResult = expect(stalled).rejects.toMatchObject({
      code: "transport_failure",
    });
    await vi.advanceTimersByTimeAsync(NATIVE_MEDIA_OPERATION_DEADLINE_MS);
    await stalledResult;
    expect(transport.pendingFrameObservations).toBe(0);
    await transport.close();
  });

  it("bounds play and re-pauses a play promise that fulfills after cancellation", async () => {
    vi.useFakeTimers();
    const pendingPlay = deferred<void>();
    const native = nativeHarness({ play: () => pendingPlay.promise });
    const { transport } = await openTransport(native);
    const initialSeek = transport.seek(seekRequest());
    native.present(0.5);
    await initialSeek;

    const abort = new AbortController();
    const playing = transport.play(abort.signal);
    abort.abort();
    await expect(playing).rejects.toMatchObject({ code: "cancelled" });
    const pausesAtCancel = native.pause.mock.calls.length;
    pendingPlay.resolve();
    await Promise.resolve();
    await Promise.resolve();
    expect(native.pause.mock.calls.length).toBeGreaterThan(pausesAtCancel);
    expect(transport.pendingFrameObservations).toBe(0);

    const never = deferred<void>();
    const secondNative = nativeHarness({ play: () => never.promise });
    const opened = await openTransport(secondNative);
    const secondSeek = opened.transport.seek(seekRequest());
    secondNative.present(0.5);
    await secondSeek;
    const stalled = opened.transport.play(new AbortController().signal);
    const stalledResult = expect(stalled).rejects.toMatchObject({
      code: "transport_failure",
    });
    await vi.advanceTimersByTimeAsync(NATIVE_MEDIA_OPERATION_DEADLINE_MS);
    await stalledResult;
    await opened.transport.close();
    await transport.close();
  });

  it("reports a playing media error once, stops observation, and removes listeners on close", async () => {
    const native = nativeHarness();
    const { transport } = await openTransport(native);
    const seeking = transport.seek(seekRequest());
    native.present(0.5);
    await seeking;
    const failures: string[] = [];
    expect(transport.subscribeFailure).toBeTypeOf("function");
    transport.subscribeFailure?.((code) => failures.push(code));
    await transport.play(new AbortController().signal);
    expect(transport.pendingFrameObservations).toBe(1);

    native.dispatch("error");
    native.dispatch("error");
    expect(failures).toEqual(["transport_failure"]);
    expect(transport.pendingFrameObservations).toBe(0);
    await transport.close();
    expect(native.listenerCount("error")).toBe(0);
    native.dispatch("error");
    expect(failures).toEqual(["transport_failure"]);
  });

  it("retains duplicate-owner exclusion until a rejected release is retried successfully", async () => {
    const native = nativeHarness();
    const firstRelease = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error("release failed"))
      .mockResolvedValueOnce(undefined);
    const duplicateRelease = vi.fn(async () => undefined);
    const finalRelease = vi.fn(async () => undefined);
    const factory = createHtmlMediaElementTransportFactory(
      async ({ ownerId }) => {
        const release =
          ownerId === "owner-a"
            ? firstRelease
            : ownerId === "owner-b"
              ? duplicateRelease
              : finalRelease;
        return {
          element: native.video,
          release,
        } satisfies HtmlMediaElementSourceOwner;
      },
    );
    const first = await factory(request("owner-a"));
    await expect(first.close()).rejects.toMatchObject({
      code: "transport_failure",
    });
    await expect(factory(request("owner-b"))).rejects.toMatchObject({
      code: "contract_mismatch",
    });
    expect(duplicateRelease).toHaveBeenCalledTimes(1);

    await expect(first.close()).resolves.toBeUndefined();
    expect(firstRelease).toHaveBeenCalledTimes(2);
    const final = await factory(request("owner-c"));
    await final.close();
    expect(finalRelease).toHaveBeenCalledTimes(1);
  });

  it("never clears the first owner decoder when a duplicate acquisition is cancelled", async () => {
    const native = nativeHarness();
    const firstRelease = vi.fn(async () => undefined);
    const secondRelease = vi.fn(async () => undefined);
    const secondAcquisition = deferred<HtmlMediaElementSourceOwner>();
    const factory = createHtmlMediaElementTransportFactory(({ ownerId }) =>
      ownerId === "owner-a"
        ? Promise.resolve({ element: native.video, release: firstRelease })
        : secondAcquisition.promise,
    );
    const first = await factory(request("owner-a"));
    const abort = new AbortController();
    const second = factory(request("owner-b", abort.signal));
    abort.abort();
    secondAcquisition.resolve({
      element: native.video,
      release: secondRelease,
    });

    await expect(second).rejects.toMatchObject({ code: "cancelled" });
    expect(secondRelease).toHaveBeenCalledTimes(1);
    expect(firstRelease).not.toHaveBeenCalled();
    expect(native.pause).not.toHaveBeenCalled();
    expect(native.removeAttribute).not.toHaveBeenCalled();
    expect(native.load).not.toHaveBeenCalled();

    const seeking = first.seek(seekRequest());
    native.present(0.5);
    await expect(seeking).resolves.toMatchObject({ observedSourcePts: 6_144 });
    await first.close();
  });

  it("cleans and releases an acquisition that completes after its request is aborted", async () => {
    const native = nativeHarness();
    const release = vi.fn(async () => undefined);
    const acquired = deferred<HtmlMediaElementSourceOwner>();
    const abort = new AbortController();
    const factory = createHtmlMediaElementTransportFactory(
      () => acquired.promise,
    );
    const opening = factory(request("owner-a", abort.signal));
    abort.abort();
    acquired.resolve({ element: native.video, release });

    await expect(opening).rejects.toMatchObject({ code: "cancelled" });
    expect(native.pause).toHaveBeenCalledTimes(1);
    expect(native.removeAttribute).toHaveBeenCalledWith("src");
    expect(native.load).toHaveBeenCalledTimes(1);
    expect(release).toHaveBeenCalledTimes(1);
  });
});
