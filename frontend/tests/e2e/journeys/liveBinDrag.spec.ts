import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  expectImportBootstrap,
  startImportFixture,
} from "../helpers/nleImportFixture";
import { shellSnapshot, shellSurface } from "../helpers/nleShell";
import {
  canonicalV2Workspace,
  snapshot,
  surface,
} from "../helpers/nleCanonical";
import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

async function beginCardDrag(page: Page, card: Locator) {
  await expect(card).toBeEnabled();
  const box = await card.boundingBox();
  if (box === null) throw new Error("media card is not rendered");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
}

async function populatedDropPoint(page: Page, kind: string, frame: number) {
  const timeline = page.locator('[data-h3-nle-region="timeline"]');
  const grid = timeline.locator(".h3-nle-tracks");
  const row = timeline.locator(`[data-h3-nle-track][data-kind="${kind}"]`);
  const [gridBox, rowBox] = await Promise.all([
    grid.boundingBox(),
    row.boundingBox(),
  ]);
  if (gridBox === null || rowBox === null)
    throw new Error("timeline drop geometry is not rendered");
  const origin = Number(
    await timeline.getAttribute("data-h3-nle-lane-origin-px"),
  );
  const scale = Number(
    await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
  );
  const start = Number(await timeline.getAttribute("data-h3-nle-view-start"));
  expect(Number.isFinite(origin) && scale > 0 && Number.isFinite(start)).toBe(
    true,
  );
  const x = gridBox.x + origin + (frame - start) * scale;
  expect(x).toBeGreaterThan(gridBox.x + origin - 1);
  expect(x).toBeLessThan(gridBox.x + gridBox.width - 2);
  return { x, y: rowBox.y + rowBox.height / 2 };
}

test("the live imported V2 card drags to Main with its actual authoring identity and one undo", async ({
  page,
}, testInfo) => {
  const fixture = await startImportFixture(page);
  try {
    await page.goto(
      "/nleShell.html?import=1&target=ready&importMedia=synthetic",
    );
    await expectImportBootstrap(fixture);
    await page
      .getByRole("navigation", { name: "H3 Context pages" })
      .getByRole("button", { name: "Production" })
      .click();
    await page.getByRole("tab", { name: "Production", exact: true }).click();
    await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
    await page.locator('[data-h3-nle-entry="open"]').click();
    await expect(page.locator(shellSurface)).toBeVisible();
    await page
      .locator('[data-h3-nle-control="asset.import_production"]')
      .click();
    await expect
      .poll(async () => (await shellSnapshot(page)).importStatus)
      .toBe("succeeded");
    await expect
      .poll(async () => (await shellSnapshot(page)).highlightedAssetIds.length)
      .toBe(1);
    const assetId = (await shellSnapshot(page)).highlightedAssetIds[0]!;
    const card = page.locator(
      `[data-h3-nle-asset="${assetId}"] .h3-nle-media-primary`,
    );
    await card.click();
    await expect
      .poll(
        async () => (await shellSnapshot(page)).authoringStateV2?.clips.length,
      )
      .toBe(1);
    await expect(card).toBeEnabled();
    const before = await shellSnapshot(page);
    expect(before.authoringStateV2).not.toBeNull();
    const source = before.authoringStateV2!.assets.find(
      (asset) => asset.assetId === assetId,
    )!;
    expect(source.sourceTimeBase).toEqual({ num: 1, den: 90000 });
    expect(source.sourceFrameCount).toBe(124);
    expect(
      source.landmarks.at(-1)!.pts + source.landmarks.at(-1)!.durationTicks,
    ).toBe(124 * 3000);
    expect(before.authoringStateV2!.timelineFingerprint).not.toBe(
      before.timelineSnapshot!.timelineFingerprint,
    );
    const transactionsBefore = fixture.transactions.length;
    const productionBefore = [...fixture.productionActions];
    const importsBefore = [...fixture.importRequests];
    const point = await populatedDropPoint(page, "primary_video", 160);
    await testInfo.attach("live-bin-before", {
      body: await page.locator(shellSurface).screenshot(),
      contentType: "image/png",
    });
    await beginCardDrag(page, card);
    await page.mouse.move(point.x, point.y, { steps: 4 });
    await page.mouse.up();
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(before.receipts + 1);
    expect(fixture.transactions).toHaveLength(transactionsBefore + 1);
    const wire = fixture.transactions.at(-1)!;
    expect(wire).toMatchObject({
      schema: "h3.context.timeline_transaction.v2",
      expected_timeline_fingerprint:
        before.authoringStateV2!.timelineFingerprint,
      expected_authoring_fingerprint:
        before.authoringStateV2!.authoringFingerprint,
      commands: [
        {
          kind: "insert_asset_clip",
          payload: {
            clip: {
              asset_id: assetId,
              start_frame: 160,
              duration_frames: 99,
              source_start_frame: 0,
            },
          },
        },
      ],
    });
    const after = await shellSnapshot(page);
    expect(after.authoringStateV2!.clips).toHaveLength(2);
    await testInfo.attach("live-bin-after", {
      body: await page.locator(shellSurface).screenshot(),
      contentType: "image/png",
    });
    await page.locator('[data-h3-nle-control="history.undo"]').click();
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(before.receipts + 2);
    expect((await shellSnapshot(page)).authoringStateV2!.clips).toEqual(
      before.authoringStateV2!.clips,
    );
    await page.locator('[data-h3-nle-control="history.redo"]').click();
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(before.receipts + 3);
    expect((await shellSnapshot(page)).authoringStateV2!.clips).toEqual(
      after.authoringStateV2!.clips,
    );
    expect(fixture.productionActions).toEqual(productionBefore);
    expect(fixture.importRequests).toEqual(importsBefore);
    expect(fixture.output.counts).toEqual({
      create: 0,
      status: 0,
      cancel: 0,
      download: 0,
    });
  } finally {
    await fixture.close();
  }
});

function catalogTracks(wire: Record<string, unknown>) {
  const assets = wire.assets as Record<string, unknown>[];
  wire.assets = [
    ...assets,
    {
      ...assets[0],
      asset_id: "picture.first",
      kind: "image",
      source_time_base: null,
      source_frame_count: null,
      source_sample_count: null,
      embedded_audio: "absent",
      timestamp_policy: "not_applicable",
      landmarks: [],
    },
  ];
  const tracks = wire.tracks as Record<string, unknown>[];
  wire.tracks = [
    ...tracks,
    {
      track_id: "track.video",
      kind: "video_overlay",
      order: 1,
      enabled: true,
      locked: false,
    },
    {
      track_id: "track.picture",
      kind: "image_overlay",
      order: 2,
      enabled: true,
      locked: false,
    },
  ];
}

async function emptyDropPoint(page: Page, kind: string, frame: number) {
  const grid = page.locator("[data-h3-nle-empty-drop]");
  const row = grid.locator(`[data-h3-nle-track][data-kind="${kind}"]`);
  const [box, rowBox] = await Promise.all([
    grid.boundingBox(),
    row.boundingBox(),
  ]);
  if (box === null || rowBox === null)
    throw new Error("empty authoring drop geometry is absent");
  const origin = Number(await grid.getAttribute("data-h3-nle-lane-origin-px"));
  const scroll = await grid.evaluate((element) => element.scrollLeft);
  const x = box.x + origin + frame - scroll;
  expect(x).toBeGreaterThanOrEqual(box.x + origin);
  expect(x).toBeLessThan(box.x + box.width);
  return { x, y: rowBox.y + rowBox.height / 2 };
}

for (const { kind, assetId, duration } of [
  { kind: "primary_video", assetId: "generated.asset.1", duration: 120 },
  { kind: "video_overlay", assetId: "generated.asset.1", duration: 120 },
  { kind: "image_overlay", assetId: "picture.first", duration: 1 },
]) {
  test(`empty V2 drops ${assetId} on ${kind} with one canonical undo and redo`, async ({
    page,
  }, testInfo) => {
    await routeGenericFixtureMedia(page);
    const transactions = await canonicalV2Workspace(
      page,
      catalogTracks,
      undefined,
      "&media=1",
    );
    const before = await snapshot(page);
    expect(before.timelineSnapshot).toBeNull();
    expect(before.authoringStateV2!.clips).toEqual([]);
    const card = page.locator(
      `[data-h3-nle-asset="${assetId}"] .h3-nle-media-primary`,
    );
    const point = await emptyDropPoint(page, kind, 18);
    await beginCardDrag(page, card);
    await page.mouse.move(point.x, point.y, { steps: 4 });
    await expect(page.locator("[data-h3-nle-insert-ghost]")).toBeVisible();
    await page.mouse.up();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
    expect(transactions).toHaveLength(1);
    expect(transactions[0]).toMatchObject({
      schema: "h3.context.timeline_transaction.v2",
      expected_timeline_fingerprint:
        before.authoringStateV2!.timelineFingerprint,
      expected_authoring_fingerprint:
        before.authoringStateV2!.authoringFingerprint,
      commands: [
        {
          kind: "insert_asset_clip",
          payload: {
            clip: {
              asset_id: assetId,
              start_frame: 18,
              duration_frames: duration,
              source_start_frame: 0,
            },
          },
        },
      ],
    });
    const after = await snapshot(page);
    expect(after.authoringStateV2!.clips).toHaveLength(1);
    expect(after.authoringStateV2!.clips[0]!.trackId).toBe(
      before.authoringStateV2!.tracks.find((track) => track.kind === kind)!
        .trackId,
    );
    expect(after.timelineSnapshot!.timelineFingerprint).not.toBe(
      after.authoringStateV2!.timelineFingerprint,
    );
    await testInfo.attach(`empty-${kind}-after`, {
      body: await page.locator(surface).screenshot(),
      contentType: "image/png",
    });
    await page.locator('[data-h3-nle-control="history.undo"]').click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
    expect((await snapshot(page)).timelineSnapshot).toBeNull();
    expect((await snapshot(page)).authoringStateV2!.clips).toEqual([]);
    await page.locator('[data-h3-nle-control="history.redo"]').click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
    expect((await snapshot(page)).authoringStateV2!.clips).toEqual(
      after.authoringStateV2!.clips,
    );
  });
}

for (const refusal of ["locked", "incompatible", "outside"] as const) {
  test(`empty V2 ${refusal} drop is visibly refused without dispatch`, async ({
    page,
  }) => {
    const transactions = await canonicalV2Workspace(page, (wire) => {
      catalogTracks(wire);
      if (refusal === "locked")
        (wire.tracks as Record<string, unknown>[])[0]!.locked = true;
    });
    const card = page.locator(
      '[data-h3-nle-asset="generated.asset.1"] .h3-nle-media-primary',
    );
    const point = await emptyDropPoint(
      page,
      refusal === "incompatible" ? "image_overlay" : "primary_video",
      18,
    );
    if (refusal === "outside") {
      const box = await page.locator("[data-h3-nle-empty-drop]").boundingBox();
      point.x = box!.x - 8;
    }
    await beginCardDrag(page, card);
    await page.mouse.move(point.x, point.y, { steps: 4 });
    const status = page.locator("[data-h3-nle-empty-drop] + p[role=status]");
    await expect(status).toContainText(
      refusal === "locked"
        ? "locked"
        : refusal === "incompatible"
          ? "can't hold this clip"
          : "track",
    );
    await page.mouse.up();
    expect(transactions).toEqual([]);
    expect((await snapshot(page)).intents).toEqual([]);
    expect((await snapshot(page)).authoringStateV2!.clips).toEqual([]);
  });
}

test("native Escape leaves no trailing card insertion and a fresh keyboard Add works", async ({
  page,
}) => {
  const transactions = await canonicalV2Workspace(page, catalogTracks);
  const card = page.locator(
    '[data-h3-nle-asset="generated.asset.1"] .h3-nle-media-primary',
  );
  const point = await emptyDropPoint(page, "primary_video", 18);
  await beginCardDrag(page, card);
  await page.mouse.move(point.x, point.y, { steps: 4 });
  await expect(page.locator("[data-h3-nle-insert-ghost]")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator(surface)).toBeVisible();
  await expect(page.locator("[data-h3-nle-insert-ghost]")).toHaveCount(0);
  const box = await card.boundingBox();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.up();
  expect(transactions).toEqual([]);
  expect((await snapshot(page)).intents).toEqual([]);
  await card.focus();
  await card.press("Enter");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  expect(transactions).toHaveLength(1);
  expect((await snapshot(page)).authoringStateV2!.clips[0]).toMatchObject({
    startFrame: 0,
    durationFrames: 120,
  });
});

test("an empty V2 near-capacity drop clips at the real scroll-axis boundary", async ({
  page,
}) => {
  const transactions = await canonicalV2Workspace(page);
  const grid = page.locator("[data-h3-nle-empty-drop]");
  await grid.evaluate((element) => {
    element.scrollLeft = element.scrollWidth;
  });
  const point = await emptyDropPoint(page, "primary_video", 3590);
  const card = page.locator(
    '[data-h3-nle-asset="generated.asset.1"] .h3-nle-media-primary',
  );
  await beginCardDrag(page, card);
  await page.mouse.move(point.x, point.y, { steps: 4 });
  await page.mouse.up();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  expect(transactions).toHaveLength(1);
  expect((await snapshot(page)).authoringStateV2!.clips[0]).toMatchObject({
    startFrame: 3590,
    durationFrames: 10,
  });
  expect((await snapshot(page)).authoringStateV2!.contentEndExclusive).toBe(
    3600,
  );
});

test("picture click and drop have identical V2 commands and authority at equal placement", async ({
  page,
}) => {
  const clicked = await canonicalV2Workspace(page, catalogTracks);
  await page
    .locator('[data-h3-nle-asset="picture.first"] .h3-nle-media-primary')
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  expect(clicked).toHaveLength(1);
  const clickedWire = clicked[0] as Record<string, unknown>;
  const dropped = await canonicalV2Workspace(page, catalogTracks);
  const card = page.locator(
    '[data-h3-nle-asset="picture.first"] .h3-nle-media-primary',
  );
  const point = await emptyDropPoint(page, "image_overlay", 0);
  await beginCardDrag(page, card);
  await page.mouse.move(point.x, point.y, { steps: 4 });
  await page.mouse.up();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  expect(dropped).toHaveLength(1);
  const droppedWire = dropped[0] as Record<string, unknown>;
  for (const key of [
    "schema",
    "authoring_schema",
    "profile_id",
    "operation_profile_id",
    "workspace_handle",
    "expected_workspace_revision",
    "expected_timeline_revision",
    "expected_timeline_fingerprint",
    "expected_authoring_fingerprint",
    "commands",
  ])
    expect(droppedWire[key]).toEqual(clickedWire[key]);
});

test("a populated V2 picture drop preserves actual authoring CAS and existing primary content", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalV2Workspace(
    page,
    catalogTracks,
    undefined,
    "&media=1",
  );
  await page
    .locator('[data-h3-nle-asset="generated.asset.1"] .h3-nle-media-primary')
    .click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const before = await snapshot(page);
  const card = page.locator(
    '[data-h3-nle-asset="picture.first"] .h3-nle-media-primary',
  );
  const point = await populatedDropPoint(page, "image_overlay", 18);
  await beginCardDrag(page, card);
  await page.mouse.move(point.x, point.y, { steps: 4 });
  await page.mouse.up();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  expect(transactions).toHaveLength(2);
  expect(transactions[1]).toMatchObject({
    expected_timeline_fingerprint: before.authoringStateV2!.timelineFingerprint,
    expected_authoring_fingerprint:
      before.authoringStateV2!.authoringFingerprint,
    commands: [
      {
        kind: "insert_asset_clip",
        payload: {
          clip: {
            asset_id: "picture.first",
            start_frame: 18,
            duration_frames: 1,
          },
        },
      },
    ],
  });
  const after = await snapshot(page);
  expect(after.authoringStateV2!.clips).toHaveLength(2);
  expect(after.authoringStateV2!.clips).toContainEqual(
    before.authoringStateV2!.clips[0],
  );
  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  expect((await snapshot(page)).authoringStateV2!.clips).toEqual(
    before.authoringStateV2!.clips,
  );
});
