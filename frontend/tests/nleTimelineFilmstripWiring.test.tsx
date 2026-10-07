import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleTimeline } from "../src/components/nle/NleTimeline";
import { useNleFilmstrips } from "../src/components/nle/useNleFilmstrips";
import { useNleWaveforms } from "../src/components/nle/useNleWaveforms";
import type { TimelineDecorationScene } from "../src/runtime/nleTimelineRaster";
import { authoringReady, SMOKE_SHAPE } from "./support/nleWorkspaceFixture";

const raster = vi.hoisted(() => ({
  read: null as (() => TimelineDecorationScene) | null,
}));
vi.mock("../src/components/nle/useNleFilmstrips", () => ({
  useNleFilmstrips: vi.fn(() => new Map()),
}));
vi.mock("../src/components/nle/useNleWaveforms", () => ({
  useNleWaveforms: vi.fn(() => new Map()),
}));
vi.mock("../src/runtime/nleTimelineRaster", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../src/runtime/nleTimelineRaster")>();
  return {
    ...actual,
    createTimelineRasterScheduler: (
      canvas: HTMLCanvasElement,
      read: () => TimelineDecorationScene,
    ) => {
      raster.read = read;
      return actual.createTimelineRasterScheduler(canvas, read);
    },
  };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("timeline filmstrip scene wiring", () => {
  it("uses accepted visible geometry and clears copied pixels synchronously when decoration is revoked", () => {
    const callbacks: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callbacks.push(callback);
      return callbacks.length;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(
      () => undefined,
    );
    const authoring = authoringReady(SMOKE_SHAPE);
    if (authoring.status !== "ready" || authoring.timelineHistory === undefined)
      throw new Error("fixture must be ready");
    const snapshot = authoring.timelineHistory.snapshot;
    const asset = snapshot.assets.find((item) => item.kind === "video")!;
    const bitmap = { close: vi.fn() } as unknown as ImageBitmap;
    vi.mocked(useNleFilmstrips).mockReturnValue(
      new Map([
        [
          asset.assetId,
          {
            bitmap,
            width: 320,
            height: 48,
            tileCount: 4,
            sourceWidth: 160,
            sourceHeight: 90,
          },
        ],
      ]),
    );
    const peaks = {
      version: 1 as const,
      pairRate: 100 as const,
      sampleRate: 8_000 as const,
      sampleCount: 8_000,
      pairCount: 100,
      pairs: new Int8Array(200),
      byteLength: 220,
    };
    vi.mocked(useNleWaveforms).mockReturnValue(
      new Map([[asset.assetId, peaks]]),
    );
    const props = {
      locale: "en" as const,
      snapshot,
      selection: [],
      authoring,
      gridFrames: 1,
      playheadFrame: 0,
      highlightedAssetIds: [],
      onIntent: vi.fn(async () => undefined),
      onEdgeGestureActive: vi.fn(),
    };
    const view = render(<NleTimeline {...props} />);
    const scene = raster.read!();
    expect(scene.filmstrips.length).toBeGreaterThan(0);
    const strip = scene.filmstrips[0]!;
    // M25-62: the clip border and selection ring are DOM (above the raster), not raster outlines;
    // the filmstrip fills the clip inside its 1 px border (row top + 4 px inset + 1 px border).
    expect("clipOutlines" in scene).toBe(false);
    expect(strip.bounds.height).toBe(46);
    expect(strip.bounds.y % 56).toBe(5);
    expect(strip.visible).toEqual({ x: 0, width: scene.width });
    expect(strip.asset.landmarks).toBe(asset.landmarks);
    expect(strip.sprite.bitmap).toBe(bitmap);
    expect(scene.waveforms.length).toBeGreaterThan(0);
    expect(
      scene.waveforms.every(({ clip }) => {
        const sourceClip = snapshot.clips.find((item) => item === clip);
        return (
          snapshot.tracks.find((track) => track.trackId === sourceClip?.trackId)
            ?.kind === "primary_video"
        );
      }),
    ).toBe(true);
    const waveform = scene.waveforms.find(({ clip }) => clip === strip.clip)!;
    expect(waveform.asset.assetId).toBe(asset.assetId);
    expect(waveform.asset.embeddedAudio).toBe("present_bound");
    expect(waveform.envelope).toBe(peaks);
    // M25-62: the waveform is the clip's bottom 14 px band.
    expect(waveform.bounds).toEqual({
      x: strip.bounds.x,
      y: strip.bounds.y + strip.bounds.height - 14,
      width: strip.bounds.width,
      height: 14,
    });
    expect(waveform.visible).toEqual(strip.visible);
    const requestedIds = vi.mocked(useNleFilmstrips).mock.calls.at(-1)![0]
      .visibleAssetIds;
    expect(
      vi.mocked(useNleWaveforms).mock.calls.at(-1)![0].visibleAssetIds,
    ).toEqual(requestedIds);
    expect(new Set(requestedIds).size).toBe(requestedIds.length);
    expect(
      requestedIds.every(
        (id) =>
          snapshot.assets.find((item) => item.assetId === id)?.kind === "video",
      ),
    ).toBe(true);
    // Initial retained-view effects can supersede a queued raster; disposed callbacks do nothing.
    for (const callback of callbacks.splice(0)) callback(0);
    const canvas = view.container.querySelector("canvas")!;
    expect(canvas.width).toBeGreaterThan(1);
    vi.mocked(useNleFilmstrips).mockReturnValue(new Map());
    vi.mocked(useNleWaveforms).mockReturnValue(new Map());
    view.rerender(<NleTimeline {...props} />);
    expect(raster.read!().filmstrips).toEqual([]);
    expect(raster.read!().waveforms).toEqual([]);
    expect(canvas.width).toBe(1);
    expect(canvas.height).toBe(1);
    expect(props.onIntent).not.toHaveBeenCalled();
  });
});
