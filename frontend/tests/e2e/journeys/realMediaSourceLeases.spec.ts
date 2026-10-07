import { expect, test } from "@playwright/test";

import type {} from "../../../e2e/mediaSourceLeases";

test("three exact-ceiling real-route proxies decode, seek, dissolve and release", async ({
  page,
}) => {
  test.skip(
    process.env.H3_CONTEXT_E2E_SERVICE_LOOPBACK !== "1" ||
      process.env.H3_CONTEXT_E2E_SERVICE_LOOPBACK_MEDIA !== "1",
    "requires the authorized media service loopback and pinned renderer pair",
  );
  test.setTimeout(180_000);

  await page.goto("/mediaSourceLeases.html");
  await page.waitForFunction(() => window.mediaSourceLeases !== undefined);
  const result = await page.evaluate(() =>
    window.mediaSourceLeases.realRouteDissolve(),
  );

  expect(result.context.container).toContain("ISO BMFF MP4");
  expect(result.context.assets).toHaveLength(3);
  expect(result.context.assets.map((asset) => asset.codec)).toEqual([
    "h264",
    "h264",
    "h264",
  ]);
  expect(result.byteCounts).toEqual([
    result.context.exactCeilingBytes,
    result.context.exactCeilingBytes,
    result.context.exactCeilingBytes,
  ]);
  expect(result.mediaTypes).toEqual(["video/mp4", "video/mp4", "video/mp4"]);
  expect(result.dimensions).toEqual([
    [1280, 720],
    [1280, 720],
    [1280, 720],
  ]);
  expect(result.requestedFrame).toBe(12);
  for (const seconds of result.presentedMediaTimes)
    expect(seconds).toBeCloseTo(result.requestedFrame / 24, 2);
  expect(result.compositionAssets).toEqual([
    "loopback-video-1",
    "loopback-video-2",
  ]);
  const product = result as typeof result & {
    compositorReceipt: {
      status: string;
      blocker: string | null;
      frame: number | null;
      renderedLayerCount: number;
    };
    compositorPath: string;
    compositionSample: number[];
    expectedBlendSample: number[];
    negativeWithoutOverlay: {
      status: string;
      blocker: string | null;
      renderedLayerCount: number;
    };
  };
  expect(product.compositorReceipt).toMatchObject({
    status: "presented",
    blocker: null,
    frame: result.requestedFrame,
    renderedLayerCount: 2,
  });
  expect(product.compositorPath).toBe("native");
  expect(product.compositionSample).toHaveLength(4);
  expect(product.expectedBlendSample).toHaveLength(4);
  for (const [index, expected] of product.expectedBlendSample.entries())
    expect(
      Math.abs(product.compositionSample[index]! - expected),
    ).toBeLessThanOrEqual(3);
  expect(product.negativeWithoutOverlay).toMatchObject({
    status: "unavailable",
    blocker: "source_unavailable",
    renderedLayerCount: 0,
  });
  expect(result.compositionIdentity).toMatch(/^sha256:[0-9a-f]{64}$/u);
  expect(result.activeOwners).toBe(2);
  expect(result.warmOwners).toBe(1);
  expect(result.liveObjectUrls).toBe(3);
  expect(result.opaquePixels).toBe(1280 * 720);

  const state = await page.evaluate(() =>
    fetch("/__loopback/media-state").then((response) => response.json()),
  );
  expect(state).toEqual({ leases: 0, cacheEntries: 0, cacheBytes: 0 });
  const counters = await page.evaluate(() =>
    fetch("/__loopback/counters").then((response) => response.json()),
  );
  expect(counters.outbound_attempts).toBe(0);
  expect(counters.media_runner_calls).toBe(result.context.fixtureRunnerCalls);
  expect(
    counters.same_origin_calls_by_route[
      "/h3-context/v1/authoring/media-source-leases"
    ],
  ).toBe(6);
  expect(
    counters.same_origin_calls_by_route[
      "/h3-context/v1/authoring/media-source-leases/open"
    ],
  ).toBe(3);
});

test("asset scope round-trips through the real route and invalid rows retain no authority", async ({
  page,
}) => {
  test.skip(
    process.env.H3_CONTEXT_E2E_SERVICE_LOOPBACK !== "1" ||
      process.env.H3_CONTEXT_E2E_SERVICE_LOOPBACK_MEDIA !== "1",
    "requires the authorized media service loopback and pinned renderer pair",
  );
  test.setTimeout(180_000);

  await page.goto("/mediaSourceLeases.html");
  await page.waitForFunction(() => window.mediaSourceLeases !== undefined);
  const result = await page.evaluate(() =>
    (
      window.mediaSourceLeases as typeof window.mediaSourceLeases & {
        realRouteAssetContract(): Promise<{
          presented: unknown;
          filmstripPresented: unknown;
          audioPeaksPresented: unknown;
          refusals: unknown;
          filmstripRefusals: unknown;
          audioPeaksRefusals: unknown;
          routeDelta: unknown;
          state: unknown;
        }>;
      }
    ).realRouteAssetContract(),
  );

  expect(result.presented).toEqual({
    scope: "asset",
    clipId: null,
    derivativeKind: "thumbnail",
    derivativeProfileId: "h3.authoring.media_derivatives.v6",
    mediaType: "image/png",
    width: 64,
    height: 36,
  });
  expect(result.filmstripPresented).toEqual({
    scope: "asset",
    clipId: null,
    derivativeKind: "filmstrip",
    derivativeProfileId: "h3.authoring.media_derivatives.v6",
    mediaType: "image/jpeg",
    byteCount: expect.any(Number),
    sourceWidth: 160,
    sourceHeight: 90,
    width: 344,
    height: 48,
    tileCount: 4,
    opaquePixels: 344 * 48,
  });
  expect(result.audioPeaksPresented).toEqual({
    derivativeKind: "audio_peaks",
    byteCount: expect.any(Number),
    sampleCount: expect.any(Number),
    pairCount: expect.any(Number),
    paintedPixels: expect.any(Number),
  });
  expect(
    (result.audioPeaksPresented as { paintedPixels: number }).paintedPixels,
  ).toBeGreaterThan(0);
  expect(result.refusals).toEqual({
    unknownScope: { status: 400, reason: "invalid_request" },
    unknownKind: { status: 422, reason: "unsupported" },
    oneByteOver: { status: 413, reason: "resource_limit" },
  });
  expect(result.filmstripRefusals).toEqual({
    backend: { status: 413, reason: "resource_limit" },
    browser: "resource_limit",
  });
  expect(result.audioPeaksRefusals).toEqual({
    invalidEnvelope: "contract_mismatch",
    overLimitBackend: { status: 413, reason: "resource_limit" },
    overLimitBrowser: "resource_limit",
  });
  expect(result.routeDelta).toEqual({ control: 15, open: 4 });
  expect(result.state).toEqual({ leases: 0, cacheEntries: 0, cacheBytes: 0 });
});
