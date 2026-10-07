import { expect, test, type Page } from "@playwright/test";

import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";
import { playheadFrame, playheadSlider } from "../helpers/nleTimeline";

const FRAME_RATE = 24;
const EXPECTED_FRAMES = 6 * FRAME_RATE;

type PixelSample = Readonly<{
  delivered: number;
  frame: number | null;
  signature: string;
  atMs: number;
}>;

async function startPixelRecorder(page: Page): Promise<void> {
  await page.evaluate(() => {
    const target = window as typeof window & {
      __m25_58_pixel_samples?: PixelSample[];
      __m25_58_pixel_stop?: () => void;
    };
    const samples: PixelSample[] = [];
    const probe = document.createElement("canvas");
    probe.width = 32;
    probe.height = 18;
    const context = probe.getContext("2d", { willReadFrequently: true });
    let lastFrame: number | null = null;
    let handle = 0;
    const tick = () => {
      const presentation = window.nleShellHarness.lastPresentation();
      const canvas = document.querySelector(
        '[data-h3-nle-canvas="composition"]',
      ) as HTMLCanvasElement | null;
      if (
        presentation !== null &&
        presentation.frame !== null &&
        presentation.frame !== lastFrame &&
        canvas !== null &&
        context !== null
      ) {
        lastFrame = presentation.frame;
        context.drawImage(canvas, 0, 0, probe.width, probe.height);
        const pixels = context.getImageData(
          0,
          0,
          probe.width,
          probe.height,
        ).data;
        let hash = 0x811c9dc5;
        for (const value of pixels) {
          hash ^= value;
          hash = Math.imul(hash, 0x01000193);
        }
        samples.push({
          delivered: presentation.delivered,
          frame: presentation.frame,
          signature: (hash >>> 0).toString(16).padStart(8, "0"),
          atMs: performance.now(),
        });
      }
      handle = requestAnimationFrame(tick);
    };
    target.__m25_58_pixel_samples = samples;
    target.__m25_58_pixel_stop = () => cancelAnimationFrame(handle);
    handle = requestAnimationFrame(tick);
  });
}

function maximumConsecutiveMisses(
  deliveredFrames: ReadonlySet<number>,
  denominator: number,
): number {
  let current = 0;
  let maximum = 0;
  for (let frame = 0; frame < denominator; frame += 1) {
    if (deliveredFrames.has(frame)) current = 0;
    else {
      current += 1;
      maximum = Math.max(maximum, current);
    }
  }
  return maximum;
}

test.use({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });

test("M25-58 product playback continuously presents the 1920x1080 composition at native 1x", async ({
  page,
}, testInfo) => {
  test.setTimeout(35_000);
  await openIntegratedShell(page, "continuous-preview-multilayer");
  await page.locator(".h3-nle-picture").evaluate((picture: HTMLDivElement) => {
    // A58-7 measures the real product compositor at the declared 1080p raster. The ordinary Fit
    // pane is intentionally smaller than the output and cannot be relabelled as a 1080p result.
    Object.assign(picture.style, {
      position: "fixed",
      inset: "0 auto auto 0",
      width: "1920px",
      height: "1080px",
      margin: "0",
      pointerEvents: "none",
      zIndex: "1000",
    });
  });
  await expect
    .poll(() =>
      page
        .locator('[data-h3-nle-canvas="composition"]')
        .evaluate(
          (canvas: HTMLCanvasElement) => `${canvas.width}x${canvas.height}`,
        ),
    )
    .toBe("1920x1080");
  const before = await shellSnapshot(page);
  expect(before.timelineSnapshot?.output.durationFrames).toBe(EXPECTED_FRAMES);
  expect(await page.evaluate(() => window.devicePixelRatio)).toBe(1);

  const primaryTrack = before.timelineSnapshot!.tracks.find(
    (track) => track.kind === "primary_video" && track.enabled,
  );
  expect(primaryTrack).toBeDefined();
  const cutFrames = before
    .timelineSnapshot!.clips.filter(
      (clip) =>
        clip.enabled &&
        clip.trackId === primaryTrack!.trackId &&
        clip.startFrame > 0,
    )
    .map((clip) => clip.startFrame)
    .sort((left, right) => left - right);
  expect(cutFrames.length).toBeGreaterThan(0);

  await page.evaluate(() => window.nleShellHarness.resetMeasurements());
  await startPixelRecorder(page);
  const startedAt = performance.now();
  await page.getByRole("button", { name: "Play", exact: true }).click();
  try {
    await expect
      .poll(() => playheadFrame(playheadSlider(page)), { timeout: 15_000 })
      .toBe(EXPECTED_FRAMES - 1);
  } catch (error) {
    const diagnostic = await page.evaluate(() => {
      const snapshot = window.nleShellHarness.snapshot();
      return {
        surfaceStatus: snapshot.surfaceStatus,
        lastPresentation: window.nleShellHarness.lastPresentation(),
        latestSamples: snapshot.presentationSamples.slice(-5),
        mediaOwnership: snapshot.mediaOwnership,
        videos: [...document.querySelectorAll("video")].map((video) => ({
          currentTime: video.currentTime,
          duration: video.duration,
          paused: video.paused,
          ended: video.ended,
          readyState: video.readyState,
          error: video.error?.code ?? null,
        })),
        sourceVideos: [
          ...(window.__nleMediaDebug?.videoElementsByClip.entries() ?? []),
        ].map(([clipId, video]) => ({
          clipId,
          currentTime: video.currentTime,
          duration: video.duration,
          paused: video.paused,
          ended: video.ended,
          readyState: video.readyState,
          error: video.error?.code ?? null,
        })),
      };
    });
    console.error(JSON.stringify(diagnostic));
    throw error;
  }
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeVisible();
  const elapsedMs = performance.now() - startedAt;

  await page.evaluate(() => {
    const target = window as typeof window & {
      __m25_58_pixel_stop?: () => void;
    };
    target.__m25_58_pixel_stop?.();
  });
  const measured = await shellSnapshot(page);
  const pixelSamples = await page.evaluate(() => {
    const target = window as typeof window & {
      __m25_58_pixel_samples?: PixelSample[];
    };
    return target.__m25_58_pixel_samples ?? [];
  });
  expect(measured.presentationSamplesDropped).toBe(0);
  const deliveredFrames = new Set(
    measured.presentationSamples.flatMap((sample) =>
      sample.frame === null ? [] : [sample.frame],
    ),
  );
  const coverageRatio = deliveredFrames.size / EXPECTED_FRAMES;
  const missingFrames = Array.from({ length: EXPECTED_FRAMES }, (_, frame) =>
    deliveredFrames.has(frame) ? null : frame,
  ).filter((frame): frame is number => frame !== null);
  const maximumMisses = maximumConsecutiveMisses(
    deliveredFrames,
    EXPECTED_FRAMES,
  );
  const layerCounts = new Set(
    measured.presentationSamples.map((sample) => sample.renderedLayerCount),
  );
  const backings = new Set(
    measured.presentationSamples.map(
      (sample) => `${sample.backingWidth}x${sample.backingHeight}`,
    ),
  );
  const distinctPixelSignatures = new Set(
    pixelSamples.map((sample) => sample.signature),
  ).size;
  const timedFrames = pixelSamples.filter(
    (sample): sample is PixelSample & { frame: number } =>
      sample.frame !== null,
  );
  const timedByFrame = new Map<number, PixelSample & { frame: number }>();
  for (const sample of timedFrames)
    if (!timedByFrame.has(sample.frame)) timedByFrame.set(sample.frame, sample);
  const steadyWindows = [
    [4, 43],
    [52, 91],
    [100, 139],
  ] as const;
  const playbackRateRatios = steadyWindows.map(([firstFrame, lastFrame]) => {
    const first = timedByFrame.get(firstFrame);
    const last = timedByFrame.get(lastFrame);
    const windowElapsedMs =
      first === undefined || last === undefined ? null : last.atMs - first.atMs;
    return {
      firstFrame,
      lastFrame,
      elapsedMs: windowElapsedMs,
      ratio:
        windowElapsedMs === null || windowElapsedMs <= 0
          ? null
          : (((lastFrame - firstFrame) / FRAME_RATE) * 1000) / windowElapsedMs,
    };
  });
  const cutCoverage = cutFrames.map((cutFrame) => ({
    cutFrame,
    nearestDistance: Math.min(
      ...[...deliveredFrames].map((frame) => Math.abs(frame - cutFrame)),
    ),
  }));
  const report = {
    denominatorFrames: EXPECTED_FRAMES,
    distinctDeliveredFrames: deliveredFrames.size,
    coverageRatio,
    missingFrames,
    maximumConsecutiveMisses: maximumMisses,
    cutCoverage,
    layerCounts: [...layerCounts].sort((left, right) => left - right),
    backings: [...backings],
    devicePixelRatio: await page.evaluate(() => window.devicePixelRatio),
    elapsedMs,
    playbackRateRatios,
    pixelSamples: pixelSamples.length,
    distinctPixelSignatures,
    finalFrame: await playheadFrame(playheadSlider(page)),
  };
  await testInfo.attach("m25-58-continuous-preview", {
    body: JSON.stringify(report),
    contentType: "application/json",
  });
  console.log(JSON.stringify(report));

  expect(coverageRatio).toBeGreaterThanOrEqual(0.95);
  expect(maximumMisses).toBeLessThanOrEqual(2);
  expect(layerCounts.has(1)).toBe(true);
  expect([...layerCounts].some((count) => count > 1)).toBe(true);
  expect([...backings]).toEqual(["1920x1080"]);
  expect(cutCoverage.every((entry) => entry.nearestDistance <= 2)).toBe(true);
  expect(playbackRateRatios.every((window) => window.ratio !== null)).toBe(
    true,
  );
  expect(
    playbackRateRatios.every(
      (window) => window.ratio! >= 0.98 && window.ratio! <= 1.02,
    ),
  ).toBe(true);
  expect(pixelSamples.length).toBeGreaterThanOrEqual(
    Math.floor(EXPECTED_FRAMES * 0.95),
  );
  expect(distinctPixelSignatures).toBeGreaterThan(1);

  await page.locator('[data-h3-nle-action="close"]').click();
  await expect
    .poll(async () => (await shellSnapshot(page)).mediaOwnership.live)
    .toBe(0);
  const afterClose = (await shellSnapshot(page)).mediaOwnership;
  expect(afterClose.acquired).toBe(afterClose.released);
  expect(afterClose.sourceLive).toBe(0);
});
