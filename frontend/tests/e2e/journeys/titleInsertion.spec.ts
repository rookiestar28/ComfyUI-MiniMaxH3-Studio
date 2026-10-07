import { expect, test } from "@playwright/test";
import type { CompositionClip } from "../../../src/contracts/compositionCodec";

import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { canonicalWorkspace, snapshot } from "../helpers/nleCanonical";
import { playheadSlider, seekPlayhead } from "../helpers/nleTimeline";
import {
  startImportFixture,
  expectImportBootstrap,
} from "../helpers/nleImportFixture";
import { shellSnapshot, shellSurface } from "../helpers/nleShell";

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

const clipsById = (clips: readonly CompositionClip[]) =>
  [...clips].sort((left, right) => left.clipId.localeCompare(right.clipId));

test("Add title allocates one text track and title in one undoable transaction", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    (wire) => {
      const tracks = wire.tracks as Record<string, unknown>[];
      const removed = new Set(
        tracks
          .filter((track) => track.kind === "text_overlay")
          .map((track) => track.track_id),
      );
      wire.tracks = tracks.filter((track) => !removed.has(track.track_id));
      wire.clips = (wire.clips as Record<string, unknown>[]).filter(
        (clip) => !removed.has(clip.track_id),
      );
    },
    undefined,
    "&media=1",
  );
  const before = (await snapshot(page)).timelineSnapshot!;
  expect(before.tracks.some((track) => track.kind === "text_overlay")).toBe(
    false,
  );
  expect(before.tracks.length).toBeLessThan(8);
  await seekPlayhead(page, playheadSlider(page), 18);
  await page.locator('[data-h3-nle-pane="text"]').click();
  await page.getByLabel("Title text", { exact: true }).fill("Opening card");
  const add = page.locator('[data-h3-nle-control="title.insert"]');
  await expect(add).toBeEnabled();
  await add.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  expect(transactions).toHaveLength(1);
  const commands = (transactions[0] as { commands: { kind: string }[] })
    .commands;
  expect(commands.map((command) => command.kind)).toEqual([
    "create_track",
    "insert_title_clip",
  ]);
  const after = (await snapshot(page)).timelineSnapshot!;
  expect(after.tracks).toHaveLength(before.tracks.length + 1);
  expect(after.clips).toHaveLength(before.clips.length + 1);
  const title = after.clips.find(
    (clip) => clip.text?.content === "Opening card",
  )!;
  expect(title).toMatchObject({ startFrame: 18, durationFrames: 24 });
  expect(
    after.tracks.find((track) => track.trackId === title.trackId)?.kind,
  ).toBe("text_overlay");
  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  expect((await snapshot(page)).timelineSnapshot!.tracks).toEqual(
    before.tracks,
  );
  // The core canonicalizes clip-array order. Undo must restore every ID and
  // property, independently of the fixture's original interleaved array order.
  expect(clipsById((await snapshot(page)).timelineSnapshot!.clips)).toEqual(
    clipsById(before.clips),
  );
  await page.locator('[data-h3-nle-control="history.redo"]').click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  expect((await snapshot(page)).timelineSnapshot!.tracks).toEqual(after.tracks);
  expect((await snapshot(page)).timelineSnapshot!.clips).toEqual(after.clips);
});

test("a full track pool explains its refusal and sends no transaction", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    (wire) => {
      wire.tracks = Array.from({ length: 8 }, (_, order) => ({
        track_id: `track.${order}`,
        kind: order === 0 ? "primary_video" : "video_overlay",
        order,
        enabled: true,
        locked: false,
      }));
      wire.clips = [];
    },
    undefined,
    "&media=1",
  );
  await page.locator('[data-h3-nle-pane="text"]').click();
  const add = page.locator('[data-h3-nle-control="title.insert"]');
  await expect(add).toBeDisabled();
  await expect(add).toHaveAccessibleDescription(
    "All eight track slots are in use. Free a text track here or remove a track.",
  );
  await expect(page.locator(".h3-nle-text-bin [role=status]")).toBeVisible();
  expect(transactions).toEqual([]);
  expect((await snapshot(page)).receipts).toBe(0);
});

test("the live empty V2 editor admits its first title and one undo returns to the empty state", async ({
  page,
}) => {
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
    const before = await shellSnapshot(page);
    expect(before.authoringStateV2?.contentEndExclusive).toBe(0);
    expect(before.timelineSnapshot).toBeNull();
    expect(
      before.authoringStateV2?.assets.some((asset) => asset.kind === "font"),
    ).toBe(true);
    const productionBefore = [...fixture.productionActions];
    await page.locator('[data-h3-nle-pane="text"]').click();
    await page.getByLabel("Title text", { exact: true }).fill("First title");
    const add = page.locator('[data-h3-nle-control="title.insert"]');
    await expect(add).toBeEnabled();
    await add.click();
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(before.receipts + 1);
    expect(fixture.transactions).toHaveLength(1);
    expect(fixture.transactions[0]).toMatchObject({
      schema: "h3.context.timeline_transaction.v2",
      expected_authoring_fingerprint:
        before.authoringStateV2!.authoringFingerprint,
      commands: [{ kind: "create_track" }, { kind: "insert_title_clip" }],
    });
    const inserted = await shellSnapshot(page);
    expect(inserted.authoringStateV2?.contentEndExclusive).toBe(24);
    expect(inserted.timelineSnapshot?.clips).toHaveLength(1);
    expect(inserted.timelineSnapshot?.clips[0]).toMatchObject({
      startFrame: 0,
      durationFrames: 24,
      text: { content: "First title" },
    });
    await page.locator('[data-h3-nle-control="history.undo"]').click();
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(before.receipts + 2);
    const undone = await shellSnapshot(page);
    expect(undone.authoringStateV2?.tracks).toEqual(
      before.authoringStateV2?.tracks,
    );
    expect(undone.authoringStateV2?.clips).toEqual([]);
    expect(undone.authoringStateV2?.contentEndExclusive).toBe(0);
    expect(undone.timelineSnapshot).toBeNull();
    await expect(add).toBeVisible();
    await expect(add).toBeEnabled();
    await page.locator('[data-h3-nle-control="history.redo"]').click();
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(before.receipts + 3);
    expect((await shellSnapshot(page)).authoringStateV2?.clips).toEqual(
      inserted.authoringStateV2?.clips,
    );
    expect(
      (await shellSnapshot(page)).authoringStateV2?.contentEndExclusive,
    ).toBe(24);
    expect(fixture.importRequests).toEqual([]);
    expect(fixture.productionActions).toEqual(productionBefore);
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
