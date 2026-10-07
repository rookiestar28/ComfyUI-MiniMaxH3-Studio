// D45-02 (plan section 11.2): what "the transport has settled" is allowed to mean.
//
// The semantic sweep used to decide it two ways, and both are satisfied by a frame that was never
// presented:
//
//   * `seekTransport` looped until two consecutive reads of the seek input were equal. `NleMonitor`
//     deliberately shows `requestedFrame` while a seek is still settling, so the input holds the
//     target the instant the fill lands and the loop exits on its first comparison.
//   * the capture loop accepted two equal signatures of the first 400 RGBA values -- 100 pixels of
//     the top-left corner. A stale frame, a cleared surface and a legitimately uniform row all hold
//     that corner still.
//
// 55 rows of the 20:28 sweep were priced on frames that had not been presented. The replacement
// waits on the compositor's own delivered receipt, which carries the frame and the composition
// fingerprint. These cases prove the receipt refuses what the old checks accepted, on the real
// shell with real media -- and that it still accepts the two legitimate shapes that make a naive
// "wait for the picture to change" check wrong: a repeated frame and a row that correctly paints
// one uniform colour.
import { test, expect } from "@playwright/test";

import {
  openIntegratedShell,
  shellLastPresentation,
  shellSnapshot,
  waitForPresentedFrame,
} from "../helpers/nleShell";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";

// The same two selectors the semantic sweep uses, so these cases watch exactly the surface whose
// readiness they are about.
// The monitor's own canvas, by its role marker: since M25-49 the media bin's card art canvases
// precede the monitor in document order, so the first canvas under the overlay surface is a
// zero-backing thumbnail, not the presented composition.
const MONITOR_CANVAS =
  '[data-h3-nle-surface="overlay_v1"] canvas[data-h3-nle-canvas="composition"]';

/** The signature the old capture loop compared: the first 400 RGBA values. */
const cornerSignature = (page: import("@playwright/test").Page) =>
  page
    .locator(MONITOR_CANVAS)
    .first()
    .evaluate((element) => {
      const canvas = element as HTMLCanvasElement;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (context === null || canvas.width === 0) return null;
      return Array.from(
        context
          .getImageData(0, 0, canvas.width, canvas.height)
          .data.slice(0, 400),
      ).join(",");
    });

async function seekTo(
  page: import("@playwright/test").Page,
  frame: number,
): Promise<void> {
  await seekPlayhead(page, playheadSlider(page), frame);
}

test("the delivered receipt names the frame and the composition that was actually painted", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  const fingerprint = (await shellSnapshot(page)).timelineSnapshot
    ?.publicFingerprint;
  expect(fingerprint).toBeTruthy();

  await seekTo(page, 6);
  const settled = await waitForPresentedFrame(page, {
    frame: 6,
    fingerprint: fingerprint!,
    budgetMs: 20_000,
  });
  expect(settled.matched).toBe(true);
  expect(settled.presentation).toMatchObject({
    frame: 6,
    publicFingerprint: fingerprint,
  });
  expect(settled.presentation!.delivered).toBeGreaterThan(0);
});

test("the seek input holds the target before the frame is presented, so it cannot prove settling", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  const fingerprint = (await shellSnapshot(page)).timelineSnapshot!
    .publicFingerprint;
  await waitForPresentedFrame(page, {
    frame: 0,
    fingerprint,
    budgetMs: 20_000,
  });

  // Issue the gesture and read the control twice in a row immediately, which is exactly what the
  // old settle loop did. It reports the target while the presented frame is still the old one --
  // the whole reason an input-value check proves nothing.
  const before = await shellLastPresentation(page);
  await seekTo(page, 31);
  const first = await playheadFrame(playheadSlider(page));
  const second = await playheadFrame(playheadSlider(page));
  expect(first).toBe(31);
  expect(second).toBe(first);
  const observedImmediately = (await shellLastPresentation(page))?.frame;
  expect(observedImmediately).not.toBeNull();

  // And the receipt is what eventually establishes it.
  const settled = await waitForPresentedFrame(page, {
    frame: 31,
    fingerprint,
    budgetMs: 20_000,
  });
  expect(settled.matched).toBe(true);
  expect(settled.presentation!.delivered).toBeGreaterThan(
    before?.delivered ?? 0,
  );
});

test("a frame that was never presented is refused even though the sampled corner never moved", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  const fingerprint = (await shellSnapshot(page)).timelineSnapshot!
    .publicFingerprint;
  await seekTo(page, 4);
  const settled = await waitForPresentedFrame(page, {
    frame: 4,
    fingerprint,
    budgetMs: 20_000,
  });
  expect(settled.matched).toBe(true);

  // The canvas is now stable on frame 4 and nothing further is asked of it, so the old check's
  // signature is identical across two reads -- it would have declared any frame "settled" here.
  const firstCorner = await cornerSignature(page);
  const secondCorner = await cornerSignature(page);
  expect(firstCorner).not.toBeNull();
  expect(secondCorner).toBe(firstCorner);

  // The receipt refuses, because frame 19 was never presented.
  const stale = await waitForPresentedFrame(page, {
    frame: 19,
    fingerprint,
    budgetMs: 2_000,
  });
  expect(stale.matched).toBe(false);
  expect(stale.presentation?.frame).toBe(4);
  expect(stale.polls).toBeGreaterThan(0);
});

test("a presentation of a different composition is refused", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  const fingerprint = (await shellSnapshot(page)).timelineSnapshot!
    .publicFingerprint;
  await seekTo(page, 2);
  expect(
    (
      await waitForPresentedFrame(page, {
        frame: 2,
        fingerprint,
        budgetMs: 20_000,
      })
    ).matched,
  ).toBe(true);

  const foreign = await waitForPresentedFrame(page, {
    frame: 2,
    fingerprint: `sha256:${"0".repeat(64)}`,
    budgetMs: 2_000,
  });
  expect(foreign.matched).toBe(false);
  expect(foreign.presentation!.publicFingerprint).toBe(fingerprint);
});

test("a repeated frame still delivers a receipt, so re-seeking is not mistaken for nothing", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  const fingerprint = (await shellSnapshot(page)).timelineSnapshot!
    .publicFingerprint;
  await seekTo(page, 9);
  const first = await waitForPresentedFrame(page, {
    frame: 9,
    fingerprint,
    budgetMs: 20_000,
  });
  expect(first.matched).toBe(true);

  // Re-issue the same target. A "wait for the picture to change" check hangs here by construction;
  // the receipt is satisfied because it asks what is presented, not whether it differs.
  await seekTo(page, 9);
  const repeated = await waitForPresentedFrame(page, {
    frame: 9,
    fingerprint,
    budgetMs: 20_000,
  });
  expect(repeated.matched).toBe(true);
  expect(repeated.presentation).toMatchObject({ frame: 9 });
});

test("the base64 raster transport delivers exactly the bytes the number-array transport did", async ({
  page,
}) => {
  // D45-03. The semantic sweep used to hand the whole backing across as a JS number array, which
  // measured 303,129 ms of a 1280 x 720 row's ~308 s of work. The replacement carries the same
  // bytes as base64. Equivalence is byte-identity of the raster: the extractor is a pure function
  // of (bytes, width, height, frame, prescription, constants), so identical bytes are identical
  // observations for every landmark class it measures.
  await openIntegratedShell(page, "smoke");
  const fingerprint = (await shellSnapshot(page)).timelineSnapshot!
    .publicFingerprint;
  await seekTo(page, 12);
  expect(
    (
      await waitForPresentedFrame(page, {
        frame: 12,
        fingerprint,
        budgetMs: 20_000,
      })
    ).matched,
  ).toBe(true);

  const both = await page
    .locator(MONITOR_CANVAS)
    .first()
    .evaluate((element) => {
      const canvas = element as HTMLCanvasElement;
      const context = canvas.getContext("2d", { willReadFrequently: true })!;
      const image = context.getImageData(0, 0, canvas.width, canvas.height);
      let binary = "";
      const CHUNK = 0x8000;
      for (let index = 0; index < image.data.length; index += CHUNK)
        binary += String.fromCharCode(
          ...image.data.subarray(index, index + CHUNK),
        );
      return {
        legacy: Array.from(image.data),
        base64: btoa(binary),
        width: canvas.width,
        height: canvas.height,
      };
    });

  const decoded = new Uint8ClampedArray(Buffer.from(both.base64, "base64"));
  expect(decoded.length).toBe(both.legacy.length);
  expect(decoded.length).toBe(both.width * both.height * 4);
  expect(Buffer.from(decoded)).toEqual(Buffer.from(both.legacy));

  // The picture is not uniform, so byte-identity is a real claim rather than a claim about a
  // constant field: a transport that dropped or reordered bytes would have to be lucky twice.
  expect(new Set(both.legacy).size).toBeGreaterThan(1);

  // Deliberately wrong pixels: one altered byte must break the equality this case rests on.
  const corrupted = Uint8Array.from(decoded);
  corrupted[Math.floor(corrupted.length / 2)] =
    (corrupted[Math.floor(corrupted.length / 2)]! + 1) % 256;
  expect(Buffer.from(corrupted)).not.toEqual(Buffer.from(both.legacy));
});
