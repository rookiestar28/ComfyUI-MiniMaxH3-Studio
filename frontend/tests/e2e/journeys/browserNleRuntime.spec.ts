import { readFileSync } from "node:fs";
import { test, expect, type Page, type CDPSession } from "@playwright/test";
import type { browserNleRuntime } from "../../../e2e/browserNleRuntime";
const vfrTiming = JSON.parse(
  readFileSync(
    new URL(
      "../../../../tests/fixtures/m25_12_runtime/timing.json",
      import.meta.url,
    ),
    "utf8",
  ),
) as { frames: Array<{ frame_index: number; pts: number }> };

declare global {
  interface Window {
    browserNleRuntime: typeof browserNleRuntime;
  }
}

// The encoded fixture is synthetic and served only through this bounded loopback interception.
// It is never admitted through a product source route or placed on a ComfyUI host.
const corpusMembers: Record<string, string> = {
  cfr: "cfr-primary.mp4",
  lane: "cfr-secondary.mp4",
  vfr: "vfr-source.mp4",
  mse: "mse-fragmented.mp4",
  invalid: "invalid.bin",
  truncated: "truncated.mp4",
};
const memorySamples = new WeakMap<
  Page,
  { session: CDPSession; baseline: number }
>();
test.beforeEach(async ({ page, context }) => {
  const session = await context.newCDPSession(page);
  await session.send("Performance.enable");
  await session.send("HeapProfiler.collectGarbage");
  const initial = await session.send("Performance.getMetrics");
  memorySamples.set(page, {
    session,
    baseline: initial.metrics.find(
      (metric) => metric.name === "JSHeapUsedSize",
    )!.value,
  });
  await page.route("**/runtime-media/*.mp4", (route) => {
    const key = new URL(route.request().url()).pathname
      .split("/")
      .at(-1)!
      .replace(".mp4", "");
    const member = corpusMembers[key];
    if (!member) return route.abort();
    const bytes = readFileSync(
      new URL(
        `../../../../tests/fixtures/m25_12_runtime/${member}`,
        import.meta.url,
      ),
    );
    const range = route.request().headers().range;
    const match = range?.match(/^bytes=(\d+)-(\d*)$/);
    const start = match ? Number(match[1]) : 0;
    const end = match?.[2]
      ? Math.min(Number(match[2]), bytes.length - 1)
      : bytes.length - 1;
    if ((range && !match) || start > end || start >= bytes.length)
      return route.fulfill({
        status: 416,
        headers: { "Content-Range": `bytes */${bytes.length}` },
      });
    // IMPORTANT: native seeking needs real byte-range semantics. Returning 200 for a Range
    // request can leave currentTime at zero and falsely implicate the frame observer.
    return route.fulfill({
      status: range ? 206 : 200,
      contentType: "video/mp4",
      headers: {
        "Accept-Ranges": "bytes",
        ...(range
          ? { "Content-Range": `bytes ${start}-${end}/${bytes.length}` }
          : {}),
      },
      body: bytes.subarray(start, end + 1),
    });
  });
});

test.afterEach(async ({ page }, testInfo) => {
  const measured = await page.evaluate(async () => {
    if (!window.browserNleRuntime) return null;
    const close = await window.browserNleRuntime.close();
    return {
      close,
      metrics: window.browserNleRuntime.metrics(),
      snapshot: window.browserNleRuntime.snapshot(),
    };
  });
  const memory = memorySamples.get(page)!;
  await memory.session.send("HeapProfiler.collectGarbage");
  const final = await memory.session.send("Performance.getMetrics");
  const jsHeapDeltaBytes = Math.max(
    0,
    final.metrics.find((metric) => metric.name === "JSHeapUsedSize")!.value -
      memory.baseline,
  );
  await memory.session.detach();
  expect(jsHeapDeltaBytes).toBeLessThanOrEqual(64 * 1024 * 1024);
  if (measured)
    await testInfo.attach("runtime-measurement", {
      body: JSON.stringify({ ...measured, jsHeapDeltaBytes }),
      contentType: "application/json",
    });
});

for (const observer of ["rvfc", "events"]) {
  test(`native ${observer} runtime seeks, repeats, plays, replaces and closes`, async ({
    page,
  }) => {
    await page.goto(`/browserNleRuntime.html?observer=${observer}`);
    await page.waitForFunction(() => window.browserNleRuntime !== undefined);
    expect(
      (await page.evaluate(() => window.browserNleRuntime.open())).status,
    ).toBe("applied");
    expect(
      await page.evaluate(() => window.browserNleRuntime.seek(12)),
    ).toMatchObject({ status: "applied" });
    expect(
      (await page.evaluate(() => window.browserNleRuntime.seek(12))).status,
    ).toBe("applied");
    expect(
      (await page.evaluate(() => window.browserNleRuntime.snapshot()))
        .sources[0]?.sourcePts,
    ).toBe(6144);
    expect(
      (await page.evaluate(() => window.browserNleRuntime.play())).status,
    ).toBe("applied");
    await expect
      .poll(
        async () =>
          page.evaluate(() =>
            window.browserNleRuntime.metrics().observedPts.at(-1),
          ),
        { timeout: 3000 },
      )
      .toBeGreaterThan(6144);
    expect(
      (await page.evaluate(() => window.browserNleRuntime.pause())).status,
    ).toBe("applied");
    expect(
      (await page.evaluate(() => window.browserNleRuntime.replace())).status,
    ).toBe("applied");
    expect(
      (await page.evaluate(() => window.browserNleRuntime.close())).status,
    ).toBe("closed");
    const metrics = await page.evaluate(() =>
      window.browserNleRuntime.metrics(),
    );
    expect(metrics.liveVideos).toBe(0);
    expect(metrics.maximumVideos).toBeLessThanOrEqual(2);
    expect(
      (await page.evaluate(() => window.browserNleRuntime.snapshot()))
        .resources,
    ).toEqual({
      activeVideoOwners: 0,
      warmVideoOwners: 0,
      canvasOwners: 0,
      pendingOperations: 0,
      pendingRvfcOwners: 0,
    });
    expect(
      (await page.evaluate(() => window.browserNleRuntime.close())).blocker,
    ).toBeNull();
  });
}

test("real CFR landmarks match decoded pixels and measured rational PTS", async ({
  page,
}) => {
  await page.goto("/browserNleRuntime.html");
  await page.waitForFunction(() => window.browserNleRuntime !== undefined);
  expect(
    (await page.evaluate(() => window.browserNleRuntime.open())).status,
  ).toBe("applied");
  for (const [frame, expected] of [
    [6, [255, 0, 0]],
    [18, [0, 0, 255]],
    [30, [255, 255, 0]],
  ] as const) {
    const result = await page.evaluate(async (at) => {
      const receipt = await window.browserNleRuntime.seek(at);
      return {
        receipt,
        source: window.browserNleRuntime.snapshot().sources[0],
        pixel:
          receipt.status === "applied" ? window.browserNleRuntime.pixel() : [],
      };
    }, frame);
    expect(result.receipt).toMatchObject({ status: "applied" });
    expect(result.source?.observation).toBe("request_video_frame_callback");
    expect(result.source?.observedSourcePts).toBe(frame * 512);
    for (let channel = 0; channel < 3; channel++)
      expect(
        Math.abs(result.pixel[channel]! - expected[channel]!),
      ).toBeLessThanOrEqual(8);
  }
});

for (const media of ["invalid", "truncated"]) {
  test(`real ${media} decode failure blocks and releases the source`, async ({
    page,
  }) => {
    await page.goto(`/browserNleRuntime.html?media=${media}`);
    await page.waitForFunction(() => window.browserNleRuntime !== undefined);
    expect(
      await page.evaluate(() => window.browserNleRuntime.open()),
    ).toMatchObject({
      status: "blocked",
      blocker: { code: "transport_failure" },
    });
    expect(
      (await page.evaluate(() => window.browserNleRuntime.metrics()))
        .liveVideos,
    ).toBe(0);
    expect(
      (await page.evaluate(() => window.browserNleRuntime.snapshot())).resources
        .activeVideoOwners,
    ).toBe(0);
  });
}

test("two actual decoders permit rapid reseek without leaking previous owners", async ({
  page,
}) => {
  await page.goto("/browserNleRuntime.html?dual=1");
  await page.waitForFunction(() => window.browserNleRuntime !== undefined);
  expect(
    (await page.evaluate(() => window.browserNleRuntime.open())).status,
  ).toBe("applied");
  expect(
    (await page.evaluate(() => window.browserNleRuntime.seek(12))).status,
  ).toBe("applied");
  expect(
    (await page.evaluate(() => window.browserNleRuntime.metrics())).liveVideos,
  ).toBe(2);
  const receipts = await page.evaluate(() =>
    Promise.all([
      window.browserNleRuntime.seek(18),
      window.browserNleRuntime.seek(19),
    ]),
  );
  expect(receipts[0]!.status).toBe("cancelled");
  expect(receipts[1]!.status).toBe("applied");
  const value = await page.evaluate(() => ({
    snapshot: window.browserNleRuntime.snapshot(),
    metrics: window.browserNleRuntime.metrics(),
  }));
  expect(value.snapshot.outputFrame).toBe(19);
  expect(value.snapshot.sources).toHaveLength(2);
  expect(value.metrics.maximumVideos).toBeLessThanOrEqual(2);
  expect(value.metrics.maximumPendingOperations).toBeLessThanOrEqual(2);
  expect(value.metrics.maximumPendingRvfc).toBeLessThanOrEqual(2);
});

test("actual VFR source uses probed presentation timestamps across a nonuniform interval", async ({
  page,
}) => {
  await page.goto("/browserNleRuntime.html?media=vfr");
  await page.waitForFunction(() => window.browserNleRuntime !== undefined);
  expect(
    await page.evaluate(() => window.browserNleRuntime.open()),
  ).toMatchObject({ status: "applied" });
  for (const frame of [22, 23, 30]) {
    expect(
      await page.evaluate((at) => window.browserNleRuntime.seek(at), frame),
    ).toMatchObject({ status: "applied" });
    const source = (
      await page.evaluate(() => window.browserNleRuntime.snapshot())
    ).sources[0]!;
    const probed = vfrTiming.frames.find((row) => row.frame_index === frame)!;
    expect(source.sourceFrame).toBe(frame);
    expect(source.sourcePts).toBe(probed.pts);
    // M25-09 qualifies VFR presentation within a source interval, not exact frame seeking.
    // Keep the measured PTS visible; do not round it to the requested anchor.
    expect(
      vfrTiming.frames.some((row) => row.pts === source.observedSourcePts),
    ).toBe(true);
    const maximumPresentationInterval = Math.max(
      ...vfrTiming.frames
        .slice(1)
        .map((row, index) => row.pts - vfrTiming.frames[index]!.pts),
    );
    expect(
      Math.abs(source.observedSourcePts! - probed.pts),
    ).toBeLessThanOrEqual(maximumPresentationInterval);
  }
  expect(vfrTiming.frames[23]!.pts - vfrTiming.frames[22]!.pts).toBe(1024);
});

test("fragmented MP4 remains on the selected intrinsic transport", async ({
  page,
}) => {
  await page.goto("/browserNleRuntime.html?media=mse");
  await page.waitForFunction(() => window.browserNleRuntime !== undefined);
  expect(
    await page.evaluate(() => window.browserNleRuntime.open()),
  ).toMatchObject({ status: "applied" });
  expect(
    await page.evaluate(() => window.browserNleRuntime.seek(12)),
  ).toMatchObject({ status: "applied" });
  expect(
    (await page.evaluate(() => window.browserNleRuntime.snapshot())).sources[0]
      ?.observedSourcePts,
  ).toBe(6144);
});

test("cancelling an actual pending media load releases within the pinned deadline", async ({
  page,
}) => {
  await page.route("**/runtime-media/cfr.mp4", () => undefined);
  await page.goto("/browserNleRuntime.html");
  await page.waitForFunction(() => window.browserNleRuntime !== undefined);
  const result = await page.evaluate(() =>
    window.browserNleRuntime.cancelPendingOpen(),
  );
  expect(result.liveBefore).toBe(1);
  expect(result.opening.status).toBe("cancelled");
  expect(result.close).toMatchObject({ status: "closed", blocker: null });
  const metrics = await page.evaluate(() => window.browserNleRuntime.metrics());
  expect(metrics.maximumCancelMs).toBeLessThanOrEqual(250);
  expect(metrics.liveVideos).toBe(0);
});
