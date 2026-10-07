// M25-16 corrective F4 / parent plan AC11: the causal Production-to-Authoring import journeys on
// the REAL integrated shell (frontend/e2e/nleShell.tsx, `import=1`) against the real registries
// and the real import service (`scripts/m25_16_import_fixture.py`). Activation by pointer and by
// keyboard; first target creation counted separately from the import; existing target reuse;
// consumed-claim refusal; catalog refresh that leaves the timeline revision alone and highlights
// the imported asset-bin row; a separate explicit insertion that changes only the timeline
// revision; Undo/Redo that traverses the *pre-import* editing branch as well as the insertion
// (post-corrective review 02, R2-F4) while the imported library row persists; and effect
// counters -- no queue, no render job, no foreign route -- for the import action.
import { test, expect, type Page } from "@playwright/test";

import {
  expectImportBootstrap,
  startImportFixture,
  type ImportFixture,
  type ImportFixtureOptions,
} from "../helpers/nleImportFixture";
import {
  shellLastPresentation,
  shellSnapshot,
  shellSurface,
} from "../helpers/nleShell";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";

const AUTHORING_ROUTE = "/h3-context/v1/authoring/action";
const PRODUCTION_ROUTE = "/h3-context/v1/production/action";
const IMPORT_ROUTE = "/h3-context/v1/production/authoring-import";
// Opening the editor performs the accepted M25-19 read-only capability GET; it starts nothing.
const CAPABILITY_ROUTE = "/h3-context/v1/authoring/output-capability";
const MEDIA_RUNTIME_STATUS_ROUTE = "/h3-context/v1/media-runtime";
const importButton = '[data-h3-nle-control="asset.import_production"]';
const assetBin = '[data-h3-nle-region="asset-bin"]';

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

async function openImportShell(
  page: Page,
  target: "absent" | "ready",
  options: ImportFixtureOptions = {},
  importMedia = "synthetic",
): Promise<ImportFixture> {
  const fixture = await startImportFixture(page, options);
  await page.goto(
    `/nleShell.html?import=1&target=${target}&importMedia=${encodeURIComponent(importMedia)}`,
  );
  try {
    await expectImportBootstrap(fixture);
  } catch (error) {
    // The caller has no handle to close yet, so a refused bootstrap would leave the process.
    await fixture.close();
    throw error;
  }
  await productionStage(page);
  await mediaStage(page);
  await expect(page.locator(importButton)).toBeEnabled();
  return fixture;
}

/** The Sidebar's Production page, then its Production workbench function tab. */
async function productionStage(page: Page): Promise<void> {
  await page
    .getByRole("navigation", { name: "H3 Context pages" })
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Production" }).click();
}

/** The same page, on the Clip editor function tab that owns the overlay launcher. */
async function clipEditorStage(page: Page): Promise<void> {
  await page
    .getByRole("navigation", { name: "H3 Context pages" })
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor" }).click();
}

/**
 * Paths an action touched beyond the three translated real routes. `since` is the fetch count
 * before the action, so only the action's own requests are attributed to it.
 */
function foreignPaths(paths: readonly string[], since: number): string[] {
  return paths
    .slice(since)
    .filter(
      (path) =>
        path !== AUTHORING_ROUTE &&
        path !== PRODUCTION_ROUTE &&
        path !== IMPORT_ROUTE &&
        path !== CAPABILITY_ROUTE,
    );
}

/**
 * The import outcome is read from the real session: a succeeded import moves focus to the Clip
 * editor function, so the Production stage's status element is no longer on screen.
 */
async function expectImport(
  page: Page,
  status: "succeeded" | "refused",
): Promise<void> {
  await expect
    .poll(async () => (await shellSnapshot(page)).importStatus)
    .toBe(status);
}

/**
 * The reads the session issues after it published the import outcome.
 *
 * IMPORTANT (M25-44 B-M2544-07): a succeeded import is published first, and the history read and
 * the catalog `read_projection` run after it (`nleWorkspaceImport.ts`: they can never relabel an
 * applied import). Comparing the fixture's action lists at the instant `succeeded` is visible races
 * those reads; wait for the recorded list instead of sampling it once.
 */
async function expectActions(
  read: () => readonly string[],
  expected: readonly string[],
): Promise<void> {
  await expect.poll(() => [...read()]).toEqual(expected);
}

/** Opens the overlay through the Sidebar's own launcher (the Clip editor function is focused). */
async function openOverlay(page: Page): Promise<void> {
  if (await page.locator(shellSurface).isVisible()) return;
  await page.locator('[data-h3-nle-entry="open"]').click();
  await expect(page.locator(shellSurface)).toBeVisible();
}

/** The re-homed import control lives in the expanded editor's Media tab. */
async function mediaStage(page: Page): Promise<void> {
  if (!(await page.locator(shellSurface).isVisible())) {
    await clipEditorStage(page);
    await openOverlay(page);
  }
  await page.locator('[data-h3-nle-pane="assets"]').click();
}

test.describe("AC11 causal import journeys", () => {
  let fixture: ImportFixture | undefined;
  test.afterEach(async () => {
    await fixture?.close();
    fixture = undefined;
  });

  test("M25-56 serves imported playback bytes only through the live timeline source lease", async ({
    page,
  }) => {
    fixture = await openImportShell(page, "absent", {}, "m25_56_tone124");
    expect(fixture.bootstrap()).toMatchObject({
      media: "m25_56_tone124",
      sourceFingerprint: expect.stringMatching(/^sha256:[0-9a-f]{64}$/),
      sourceByteLength: expect.any(Number),
    });

    await page.locator(importButton).click();
    await expect
      .poll(async () => (await shellSnapshot(page)).importStatus)
      .toMatch(/^(succeeded|refused)$/);
    const importOutcome = await shellSnapshot(page);
    expect(
      importOutcome.importStatus,
      JSON.stringify({
        refusal: importOutcome.importRefusal,
        requests: fixture.importRequests,
        payloads: fixture.importPayloads,
      }),
    ).toBe("succeeded");
    const imported = importOutcome;
    expect(imported.highlightedAssetIds).toHaveLength(1);
    const assetId = imported.highlightedAssetIds[0]!;
    expect(fixture.importedAssetIds()).toContain(assetId);
    // IMPORTANT: "only through the live timeline source lease" has two halves. An imported
    // source that no clip uses yet belongs to an asset-only workspace, which has no render
    // snapshot, so the render-source lease refuses it and no playback bytes may be served. Do
    // not restore a 200 here and do not delete the refusal: a 200 before the insertion would mean
    // playback media is reachable outside the placed timeline. The bytes are pinned after it.
    expect(imported.timelineSnapshot).toBeNull();
    expect(
      imported.authoringStateV2?.assets
        .filter((asset) => asset.kind !== "font")
        .map((asset) => asset.assetId),
    ).toEqual([assetId]);
    const beforeInsertion = await fixture.requestMediaSource(assetId);
    expect(beforeInsertion).toEqual({
      status: 409,
      error: "render_snapshot_unavailable",
    });
    expect(fixture.mediaSourceRequests()).toEqual([]);

    const row = page.locator(`${assetBin} [data-h3-nle-asset="${assetId}"]`);
    await expect(row).toBeVisible();
    await row.locator('[data-h3-nle-control="asset.insert"]').click();
    await expect.poll(() => fixture!.transactions.length).toBe(1);
    await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
    expect(fixture.importPayloads[0]?.authoring_workspace_handle).toBe(
      fixture.transactions[0]?.workspace_handle,
    );
    const afterInsertion = await fixture.requestMediaSource(assetId);
    expect(
      afterInsertion.status,
      JSON.stringify({
        afterInsertion,
        assetId,
        importedAssetIds: fixture.importedAssetIds(),
        transactions: fixture.transactions,
      }),
    ).toBe(200);
    expect(afterInsertion.assetId).toBe(assetId);
    expect(afterInsertion.sourceFingerprint).toBe(
      fixture.bootstrap()!.sourceFingerprint,
    );
    expect(afterInsertion.byteLength).toBe(
      fixture.bootstrap()!.sourceByteLength,
    );
    await expect
      .poll(() => fixture!.mediaSourceRequests().length)
      .toBeGreaterThan(0);

    const served = fixture.mediaSourceRequests()[0]!;
    expect(served.assetId).toBe(assetId);
    expect(served.sourceFingerprint).toBe(
      fixture.bootstrap()!.sourceFingerprint,
    );
    expect(served.byteLength).toBe(fixture.bootstrap()!.sourceByteLength);

    const play = page.locator('[data-h3-nle-control="transport.play"]');
    await expect(play).toBeEnabled();
    await play.click();
    await expect
      .poll(async () => shellLastPresentation(page), { timeout: 10_000 })
      .toMatchObject({ frame: expect.any(Number) });
    await page.waitForTimeout(500);
    const later = await shellLastPresentation(page);
    expect(later).not.toBeNull();
    expect(later!.frame).toBeGreaterThan(0);
    await page.locator('[data-h3-nle-control="transport.pause"]').click();
    await expect
      .poll(() => fixture!.mediaSourceRequests().length)
      .toBeGreaterThan(0);
  });

  test("M25-56 classifies the two-source duration, ruler zoom, and clip-selection baseline", async ({
    page,
  }, testInfo) => {
    fixture = await startImportFixture(page);
    await page.goto(
      "/nleShell.html?import=1&target=absent&segments=2&importMedia=m25_56_tone124",
    );
    await expectImportBootstrap(fixture);
    await productionStage(page);
    await mediaStage(page);
    await expect(page.locator(importButton)).toBeEnabled();
    await page.locator(importButton).click();
    await expectImport(page, "succeeded");

    const imported = await shellSnapshot(page);
    expect(imported.highlightedAssetIds).toHaveLength(2);
    const assetId = imported.highlightedAssetIds[0]!;
    // Both sources are in the authoring catalog; an asset-only workspace has no render snapshot
    // to read them from until the insertion below places one.
    expect(imported.timelineSnapshot).toBeNull();
    expect(
      imported.authoringStateV2?.assets
        .filter((asset) => asset.kind !== "font")
        .map((asset) => asset.assetId)
        .sort(),
    ).toEqual([...imported.highlightedAssetIds].sort());
    await page
      .locator(`${assetBin} [data-h3-nle-asset="${assetId}"]`)
      .locator('[data-h3-nle-control="asset.insert"]')
      .click();
    await expect.poll(() => fixture!.transactions.length).toBe(1);
    await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
    const insertionTransactionCount = fixture.transactions.length;

    const inserted = await shellSnapshot(page);
    const importedClips = inserted.timelineSnapshot!.clips.filter(
      (clip) =>
        clip.assetId !== null &&
        imported.highlightedAssetIds.includes(clip.assetId),
    );
    expect(importedClips.length).toBeGreaterThanOrEqual(1);
    const contentEndExclusive = Math.max(
      ...importedClips.map((clip) => clip.startFrame + clip.durationFrames),
    );
    const selectedClip = importedClips[0]!;
    const selectedElement = page.locator(
      `[data-h3-nle-clip="${selectedClip.clipId}"]`,
    );
    await expect(
      page.locator('[data-h3-nle-control="transport.play"]'),
    ).toBeEnabled({ timeout: 10_000 });
    const slider = playheadSlider(page);
    await seekPlayhead(page, slider, 48);
    const d6FrameBeforeSelection = await playheadFrame(slider);
    const d6PresentationBeforeSelection = await shellLastPresentation(page);
    const receiptsBeforeSelection = (await shellSnapshot(page)).receipts;
    const transactionsBeforeSelection = fixture.transactions.length;
    await selectedElement.click();
    await expect
      .poll(() => fixture!.transactions.length)
      .toBe(transactionsBeforeSelection + 1);
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(receiptsBeforeSelection + 1);
    await expect(selectedElement).toHaveAttribute("data-selected", "true");
    await expect
      .poll(async () => (await shellSnapshot(page)).authoringStatus)
      .toBe("ready");
    const d6FrameAfterSelection = await playheadFrame(slider);
    const d6PresentationAfterSelection = await shellLastPresentation(page);
    const d6Transaction = fixture.transactions.at(-1);
    expect(d6Transaction).toMatchObject({
      commands: [{ kind: "select_clips" }],
    });
    const selectedClipIdsAfterClick = await page
      .locator('[data-h3-nle-clip][data-selected="true"]')
      .evaluateAll((elements) =>
        elements.map((element) => element.getAttribute("data-h3-nle-clip")),
      );

    const timeline = page.locator('[data-h3-nle-region="timeline"]');
    const scaleBeforeZoom = Number(
      await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
    );
    await page.locator('[data-h3-nle-control="transport.zoom_in"]').click();
    const scaleAfterZoom = Number(
      await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
    );
    expect(scaleAfterZoom).toBeGreaterThan(scaleBeforeZoom);
    const playhead = page.locator(".h3-nle-playhead");
    const playheadState = await playhead.evaluate((element) => ({
      hidden: (element as HTMLElement).hidden,
      left: (element as HTMLElement).style.left,
      color: getComputedStyle(element).backgroundColor,
    }));
    const rulerLabels = await page
      .locator(".h3-nle-ruler-mark")
      .allTextContents();
    const classified = {
      schema: "M25-56NleUxBaselineTraceV1",
      fixture: {
        media: fixture.bootstrap()?.media,
        sourceFingerprint: fixture.bootstrap()?.sourceFingerprint,
        sourceByteLength: fixture.bootstrap()?.sourceByteLength,
        importedAssets: imported.highlightedAssetIds,
      },
      duration: {
        outputDurationFrames: inserted.timelineSnapshot!.output.durationFrames,
        contentEndExclusive,
        importedClipCount: importedClips.length,
        importedSourceCount: imported.highlightedAssetIds.length,
        requestedInsertCount: 1,
        acceptedInsertTransactions: insertionTransactionCount,
        disposition:
          inserted.timelineSnapshot!.output.durationFrames ===
          contentEndExclusive
            ? "NOT_REPRODUCED_CONTENT_EXTENT_MATCHED"
            : "RED_OUTPUT_EXTENT_EXCEEDS_PLACED_IMPORTED_CLIP",
      },
      rulerZoom: {
        rulerLabels,
        scaleBeforeZoom,
        scaleAfterZoom,
        fullHeightGridLineCount: await page
          .locator(".h3-nle-grid-line")
          .count(),
        disposition:
          scaleAfterZoom > scaleBeforeZoom
            ? "NOT_REPRODUCED_ZOOM_CHANGES_SCALE"
            : "RED_ZOOM_DID_NOT_CHANGE_SCALE",
      },
      selectionPlayhead: {
        selectedClipId: selectedClip.clipId,
        selectedClipIds: selectedClipIdsAfterClick,
        playhead: playheadState,
        disposition: playheadState.hidden
          ? "RED_SELECTED_CLIP_WITHOUT_VISIBLE_PLAYHEAD"
          : "NOT_REPRODUCED_VISIBLE_PLAYHEAD_PRESENT",
      },
      d6SelectionReplacement: {
        requestedFrame: 48,
        frameBeforeSelection: d6FrameBeforeSelection,
        frameAfterSelection: d6FrameAfterSelection,
        presentationBeforeSelection: d6PresentationBeforeSelection,
        presentationAfterSelection: d6PresentationAfterSelection,
        transaction: d6Transaction,
        transactionsBeforeSelection,
        transactionsAfterSelection: fixture.transactions.length,
        receiptsBeforeSelection,
        receiptsAfterSelection: (await shellSnapshot(page)).receipts,
        disposition:
          d6FrameAfterSelection === d6FrameBeforeSelection
            ? "NOT_REPRODUCED_SELECTION_REPLACEMENT_PRESERVED_FRAME"
            : d6FrameAfterSelection === 0
              ? "RED_SELECTION_REPLACEMENT_RESET_TO_FRAME_ZERO"
              : "RED_SELECTION_REPLACEMENT_CHANGED_FRAME_UNEXPECTEDLY",
      },
    };
    await testInfo.attach("m25-56-nle-ux-baseline-trace", {
      contentType: "application/json",
      body: Buffer.from(JSON.stringify(classified)),
    });
    console.log(`M25-56_NLE_UX_BASELINE=${JSON.stringify(classified)}`);
    expect(classified.fixture).toMatchObject({
      media: "m25_56_tone124",
      sourceFingerprint: expect.stringMatching(/^sha256:[0-9a-f]{64}$/),
      sourceByteLength: expect.any(Number),
    });
    expect(rulerLabels.length).toBeGreaterThan(1);
  });

  test("pointer activation creates the first Authoring target, imports once, refreshes the catalog and highlights the row", async ({
    page,
  }) => {
    fixture = await openImportShell(page, "absent");
    const before = await shellSnapshot(page);
    await page.locator(importButton).click();
    await expectImport(page, "succeeded");

    // Project-owned target ensure and history initialization precede the single import;
    // the one history read afterwards refreshes the catalog.
    const first = fixture;
    await expectActions(
      () => first.authoringActions,
      [
        "ensure_authoring_from_production",
        "initialize_timeline_history",
        "read_timeline_history",
      ],
    );
    expect(fixture.importRequests).toHaveLength(1);
    await expectActions(() => first.productionActions, ["read_projection"]);
    expect(fixture.transactions).toHaveLength(0);

    const imported = await shellSnapshot(page);
    expect(imported.importStatus).toBe("succeeded");
    expect(imported.highlightedAssetIds).toHaveLength(1);
    // Catalog refresh: empty V2 authoring has no render snapshot, but its workspace identity
    // advances while the timeline revision stays at the initialized empty-authoring baseline.
    expect(imported.timelineSnapshot).toBeNull();
    expect(imported.authoringStateV2?.timelineRevision).toBe(1);
    expect(imported.authoringStateV2?.workspaceRevision).toBe(2);
    expect(
      imported.authoringStateV2?.assets.map((asset) => asset.assetId),
    ).toContain(imported.highlightedAssetIds[0]);
    // Effect counters for the import action: nothing queued, no render job, no foreign route.
    expect(imported.queuedPrompts).toBe(0);
    expect(imported.renderJobRequests).toBe(0);
    const foreign = foreignPaths(imported.fetchPaths, before.fetchPaths.length);
    expect(foreign).toEqual([]);

    // The imported library row is highlighted in the opened editor's asset bin.
    await openOverlay(page);
    const assetId = imported.highlightedAssetIds[0]!;
    const row = page.locator(`${assetBin} [data-h3-nle-asset="${assetId}"]`);
    await expect(row).toBeVisible();
    // The rebuilt media bin marks an added or imported card with its art badge.
    await expect(
      row.locator('[data-h3-nle-media-badge="added"]'),
    ).toBeVisible();
  });

  test("keyboard activation reuses the existing Authoring target and a second import of the same output is idempotent without touching the timeline", async ({
    page,
  }) => {
    fixture = await openImportShell(page, "ready");
    const before = await shellSnapshot(page);
    await page.locator(importButton).focus();
    await page.keyboard.press("Enter");
    await expectImport(page, "succeeded");

    // Existing target reuse checks live history before import, then refreshes the catalog.
    const reused = fixture;
    await expectActions(
      () => reused.authoringActions,
      [
        "ensure_authoring_from_production",
        "read_timeline_history",
        "read_timeline_history",
      ],
    );
    expect(fixture.importRequests).toHaveLength(1);
    const imported = await shellSnapshot(page);
    expect(imported.authoringWorkspaceHandle).toBe(
      before.authoringWorkspaceHandle,
    );
    expect(imported.authoringStateV2?.timelineRevision).toBe(
      before.authoringStateV2?.timelineRevision,
    );
    expect(imported.authoringStateV2?.workspaceRevision).toBe(
      (before.authoringStateV2?.workspaceRevision ?? 0) + 1,
    );
    expect(imported.authoringStateV2?.assets.length).toBe(
      (before.authoringStateV2?.assets.length ?? 0) + 1,
    );
    expect(foreignPaths(imported.fetchPaths, before.fetchPaths.length)).toEqual(
      [],
    );
    expect(imported.queuedPrompts).toBe(0);

    // Consumed claim, as the accepted M25-29 contract actually states it: the same ready output
    // is not imported twice. A second import with fresh identities succeeds idempotently with the
    // row dispositioned `already_imported`; no new asset, no workspace or timeline revision
    // change, and nothing replayed as a transaction. (A stale second request is the separate
    // 409 `conflict_or_replay` branch covered at the unit seam.)
    await mediaStage(page);
    const beforeSecond = await shellSnapshot(page);
    await page.locator(importButton).click();
    await expect.poll(async () => fixture!.importRequests.length).toBe(2);
    await expectImport(page, "succeeded");
    await expect
      .poll(async () => (await shellSnapshot(page)).importDispositions)
      .toEqual(["already_imported"]);
    expect(fixture.importRequests[0]).not.toBe(fixture.importRequests[1]);
    expect(fixture.transactions).toHaveLength(0);
    const second = await shellSnapshot(page);
    expect(second.authoringStateV2?.workspaceRevision).toBe(
      imported.authoringStateV2?.workspaceRevision,
    );
    expect(second.authoringStateV2?.timelineRevision).toBe(
      imported.authoringStateV2?.timelineRevision,
    );
    expect(second.authoringStateV2?.assets.length).toBe(
      imported.authoringStateV2?.assets.length,
    );
    expect(second.highlightedAssetIds).toEqual(imported.highlightedAssetIds);
    expect(
      foreignPaths(second.fetchPaths, beforeSecond.fetchPaths.length),
    ).toEqual([]);
    expect(second.queuedPrompts).toBe(0);
  });

  test("Undo/Redo traverse the pre-import edit and the insertion while the imported library row persists", async ({
    page,
  }) => {
    // R2-F4: undoing only the post-import insertion proves retention, not preservation of an
    // older branch. The journey therefore makes an accepted edit A *before* the import and runs
    // edit A -> import -> insert B -> Undo B -> Undo A -> Redo A -> Redo B, asserting content at
    // every transition rather than counts alone.
    fixture = await openImportShell(page, "ready");
    const before = await shellSnapshot(page);
    expect(before.timelineSnapshot).toBeNull();
    const baseTracks = before.authoringStateV2!.tracks.length;
    const baseRevision = before.authoringStateV2!.timelineRevision;

    // Edit A: empty V2 authoring deliberately has no rendered track menu. Use the harness's
    // real shell dispatch seam for setup so the command still traverses the production action,
    // registry, receipt and history path before the import interaction under test.
    const addedTrackId = "pre-import-video-track";
    await page.evaluate(
      ({ trackId }) =>
        window.nleShellHarness.applyTimelineCommands([
          {
            kind: "create_track",
            payload: { track_id: trackId, kind: "video_overlay", order: 0 },
          },
        ]),
      { trackId: addedTrackId },
    );
    await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
    const edited = await shellSnapshot(page);
    expect(edited.authoringStateV2?.tracks).toHaveLength(baseTracks + 1);
    expect(edited.authoringStateV2?.timelineRevision).toBe(baseRevision + 1);
    expect(
      edited.authoringStateV2?.tracks.map((track) => track.trackId),
    ).toContain(addedTrackId);
    await page.locator('[data-h3-nle-action="close"]').click();
    await expect(page.locator(shellSurface)).toBeHidden();

    // The import is not a timeline edit: it advances the workspace, never the timeline.
    await mediaStage(page);
    await page.locator(importButton).click();
    await expectImport(page, "succeeded");
    const imported = await shellSnapshot(page);
    const assetId = imported.highlightedAssetIds[0]!;
    expect(imported.authoringStateV2?.timelineRevision).toBe(baseRevision + 1);
    expect(imported.authoringStateV2?.workspaceRevision).toBe(
      (edited.authoringStateV2?.workspaceRevision ?? 0) + 1,
    );
    await openOverlay(page);
    const assetRow = page.locator(
      `${assetBin} [data-h3-nle-asset="${assetId}"]`,
    );
    await expect(assetRow).toBeVisible();

    // Insert B: one transaction, one receipt, one clip on the imported asset.
    await assetRow.locator('[data-h3-nle-control="asset.insert"]').click();
    await expect.poll(() => fixture!.transactions.length).toBe(2);
    await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);
    const inserted = await shellSnapshot(page);
    expect(
      inserted.authoringStateV2?.clips.filter(
        (clip) => clip.assetId === assetId,
      ),
    ).toHaveLength(1);
    expect(inserted.authoringStateV2?.timelineRevision).toBe(baseRevision + 2);
    expect(inserted.authoringStateV2?.assets.length).toBe(
      imported.authoringStateV2?.assets.length,
    );

    const undo = page.locator('[data-h3-nle-control="history.undo"]');
    const redo = page.locator('[data-h3-nle-control="history.redo"]');
    const step = async (
      control: typeof undo,
      receipts: number,
    ): Promise<Awaited<ReturnType<typeof shellSnapshot>>> => {
      await control.click();
      await expect
        .poll(async () => (await shellSnapshot(page)).receipts)
        .toBe(receipts);
      return shellSnapshot(page);
    };

    // Undo B: the insertion is gone, edit A survives, the library keeps the imported asset.
    const undoneB = await step(undo, 3);
    expect(undoneB.authoringStateV2?.clips).toHaveLength(0);
    expect(undoneB.authoringStateV2?.tracks).toEqual(
      edited.authoringStateV2?.tracks,
    );
    expect(
      undoneB.authoringStateV2?.assets.map((asset) => asset.assetId),
    ).toContain(assetId);
    await expect(assetRow).toBeVisible();
    await expect(undo).toBeEnabled();

    // Undo A: the branch that precedes the import is reachable, and crossing that boundary
    // still does not evict the imported asset or re-run the import.
    const undoneA = await step(undo, 4);
    expect(
      undoneA.authoringStateV2?.tracks.map((track) => track.trackId),
    ).not.toContain(addedTrackId);
    expect(undoneA.authoringStateV2?.tracks).toEqual(
      before.authoringStateV2?.tracks,
    );
    expect(undoneA.authoringStateV2?.clips).toEqual(
      before.authoringStateV2?.clips,
    );
    expect(
      undoneA.authoringStateV2?.assets.map((asset) => asset.assetId),
    ).toContain(assetId);
    await expect(assetRow).toBeVisible();
    expect(fixture.importRequests).toHaveLength(1);
    // The history cursor is back at the branch root the workspace started from: there is
    // nothing older to undo, and the import never became an undoable timeline entry.
    await expect(undo).toBeDisabled();
    await expect(redo).toBeEnabled();

    // Redo A then Redo B restore the same content in the same order.
    const redoneA = await step(redo, 5);
    expect(
      redoneA.authoringStateV2?.tracks.map((track) => track.trackId),
    ).toContain(addedTrackId);
    expect(redoneA.authoringStateV2?.tracks).toEqual(
      edited.authoringStateV2?.tracks,
    );
    expect(redoneA.authoringStateV2?.clips).toHaveLength(0);
    const redoneB = await step(redo, 6);
    expect(redoneB.authoringStateV2?.clips).toEqual(
      inserted.authoringStateV2?.clips,
    );
    expect(redoneB.authoringStateV2?.tracks).toEqual(
      inserted.authoringStateV2?.tracks,
    );
    await expect(redo).toBeDisabled();
    // Undo and Redo are themselves accepted transactions, so the timeline revision keeps
    // advancing even where the content returns to an earlier state. Identity is the content
    // and the receipt chain, never the counter.
    expect(redoneB.authoringStateV2!.timelineRevision).toBeGreaterThan(
      inserted.authoringStateV2!.timelineRevision,
    );

    // The import stayed outside the timeline history: exactly the six edits above were
    // transacted, in order, and no Undo/Redo re-ran the import or created a second target.
    expect(
      fixture.transactions.map((transaction) =>
        (transaction.commands as { kind: string }[]).map(
          (command) => command.kind,
        ),
      ),
    ).toEqual([
      ["create_track"],
      ["insert_asset_clip"],
      ["undo"],
      ["undo"],
      ["redo"],
      ["redo"],
    ]);
    expect(fixture.importRequests).toHaveLength(1);
    expect(fixture.authoringActions).not.toContain(
      "create_authoring_workspace",
    );
    expect(redoneB.authoringWorkspaceHandle).toBe(
      before.authoringWorkspaceHandle,
    );
    // The Media-bin decoration path owns no runtime setup or final-output capability side effect;
    // the V2 surface performs only its read-only media-runtime status probe across this journey.
    const foreign = foreignPaths(redoneB.fetchPaths, before.fetchPaths.length);
    expect(foreign).toEqual([MEDIA_RUNTIME_STATUS_ROUTE]);
    expect(redoneB.queuedPrompts).toBe(0);
    expect(redoneB.renderJobRequests).toBe(0);
  });

  // M25-40 AC40-03, integrated with the real registries: a concurrent Production change makes the
  // captured revision stale, so the real import route refuses with 409. The session reconciles
  // once (Production read, same-project editor ensure and history read), recaptures the exact
  // same output and editor with the moved revision, and imports under a new request identity.
  test("a known stale Production revision recaptures once with the exact output and editor, then imports", async ({
    page,
  }, testInfo) => {
    const touched: number[] = [];
    fixture = await openImportShell(page, "absent", {
      beforeDispatch: async (kind, index, control) => {
        if (kind === "import" && index === 0)
          touched.push(await control.touchProduction());
        // B-M2544-07: hold the history read that follows the accepted import, so the catalog
        // read after it always lands well after `succeeded` is visible. The expectations below
        // must wait for those reads rather than sample the lists when the outcome appears.
        if (kind === "authoring" && index === 4)
          await new Promise((resolve) => setTimeout(resolve, 750));
      },
    });
    const before = await shellSnapshot(page);
    await page.locator(importButton).click();
    await expectImport(page, "succeeded");

    expect(touched).toHaveLength(1);
    expect(fixture.importRequests).toHaveLength(2);
    expect(fixture.importRequests[1]).not.toBe(fixture.importRequests[0]);
    const [refused, recaptured] = fixture.importPayloads as [
      Record<string, unknown>,
      Record<string, unknown>,
    ];
    // Exactly the Production identity moved; the selected output and the editor did not.
    expect(recaptured.entries).toEqual(refused.entries);
    expect(recaptured.authoring_workspace_handle).toBe(
      refused.authoring_workspace_handle,
    );
    expect(recaptured.production_workspace_handle).toBe(
      refused.production_workspace_handle,
    );
    expect(refused.expected_production_workspace_revision).toBe(
      touched[0]! - 1,
    );
    expect(recaptured.expected_production_workspace_revision).toBe(touched[0]);
    const attempts = fixture.importAttempts();
    expect(attempts).toEqual([
      {
        requestId: fixture.importRequests[0],
        expectedProductionWorkspaceRevision: touched[0]! - 1,
        httpStatus: 409,
        receiptRequestId: null,
        returnedProductionWorkspaceRevision: null,
      },
      {
        requestId: fixture.importRequests[1],
        expectedProductionWorkspaceRevision: touched[0],
        httpStatus: 200,
        receiptRequestId: fixture.importRequests[1],
        returnedProductionWorkspaceRevision: touched[0],
      },
    ]);
    // One cold ensure and initialization, one refusal reconciliation, one catalog refresh.
    const recapture = fixture;
    await expectActions(
      () => recapture.authoringActions,
      [
        "ensure_authoring_from_production",
        "initialize_timeline_history",
        "ensure_authoring_from_production",
        "read_timeline_history",
        "read_timeline_history",
      ],
    );
    await expectActions(
      () => recapture.productionActions,
      ["read_projection", "read_projection"],
    );
    expect(fixture.transactions).toHaveLength(0);

    const imported = await shellSnapshot(page);
    const o1Evidence = {
      schema: "M25-56O1CorrelatedImportTraceV1",
      intent: "explicit_production_output_import",
      attempts,
      finalNotice: {
        status: imported.importStatus,
        typedRefusal: imported.importRefusal,
      },
      disposition:
        "NOT_REPRODUCED_MATCHED_REFUSAL_SUPERSEDED_BY_MATCHED_SUCCESS",
    };
    await testInfo.attach("m25-56-o1-correlated-import-trace", {
      contentType: "application/json",
      body: Buffer.from(JSON.stringify(o1Evidence)),
    });
    console.log(`M25-56_O1_TRACE=${JSON.stringify(o1Evidence)}`);
    expect(imported.importStatus).toBe("succeeded");
    expect(imported.importRefusal).toBeNull();
    expect(imported.highlightedAssetIds).toHaveLength(1);
    // The recaptured import lands in the authoring catalog exactly as an unrefused one does: the
    // workspace identity advances, the timeline revision stays at the initialized baseline, and
    // an asset-only workspace has no render snapshot to read the catalog from.
    expect(imported.timelineSnapshot).toBeNull();
    expect(imported.authoringStateV2?.timelineRevision).toBe(1);
    expect(imported.authoringStateV2?.workspaceRevision).toBe(2);
    expect(
      imported.authoringStateV2?.assets.map((asset) => asset.assetId),
    ).toContain(imported.highlightedAssetIds[0]);
    expect(imported.queuedPrompts).toBe(0);
    expect(imported.renderJobRequests).toBe(0);
    expect(foreignPaths(imported.fetchPaths, before.fetchPaths.length)).toEqual(
      [],
    );
  });

  // M25-40 AC40-03 negative: when the recaptured request is refused again, recovery is spent.
  // There is no third import, no recursive refresh loop and no new identity without a new
  // explicit action; the refusal stays visible and the editor holds no imported asset.
  test("a second known refusal ends recovery without a third import or a loop", async ({
    page,
  }) => {
    // Hold the first reconciliation so a transient refusal cannot accidentally look settled.
    let releaseProjection!: () => void;
    const projectionReady = new Promise<void>((resolve) => {
      releaseProjection = resolve;
    });
    fixture = await openImportShell(page, "absent", {
      beforeDispatch: async (kind, index, control) => {
        if (kind === "import") await control.touchProduction();
        if (kind === "production" && index === 0) await projectionReady;
      },
    });
    await page.locator(importButton).click();
    await expectImport(page, "refused");
    await expect
      .poll(async () => (await shellSnapshot(page)).importRefusal)
      .toBe("conflict_or_replay");
    releaseProjection();
    // IMPORTANT: refusal is published before the one bounded recapture. Wait for that second
    // request and its refusal, not the identical first refusal that precedes reconciliation.
    await expect.poll(() => fixture?.importRequests.length).toBe(2);
    await expectImport(page, "refused");
    await expect
      .poll(async () => (await shellSnapshot(page)).importRefusal)
      .toBe("conflict_or_replay");
    // A settled refusal must stay settled: no delayed recovery may post again.
    await page.waitForTimeout(1_000);
    expect(fixture.importRequests).toHaveLength(2);
    expect(fixture.authoringActions).toEqual([
      "ensure_authoring_from_production",
      "initialize_timeline_history",
      "ensure_authoring_from_production",
      "read_timeline_history",
    ]);
    expect(fixture.productionActions).toEqual([
      "read_projection",
      "read_projection",
    ]);
    const settled = await shellSnapshot(page);
    expect(settled.importStatus).toBe("refused");
    expect(settled.highlightedAssetIds).toEqual([]);
    // The initialized editor holds only its bundled font asset; nothing was imported.
    // IMPORTANT: read the authoring catalog, and require it to be there. This workspace has no
    // placed content, so its render snapshot is null whether or not an asset was imported: a read
    // through `timelineSnapshot?.assets ?? []` is empty for every catalog and proves nothing.
    expect(settled.timelineSnapshot).toBeNull();
    expect(settled.authoringStateV2).not.toBeNull();
    expect(
      settled.authoringStateV2!.assets.filter((asset) => asset.kind !== "font"),
    ).toEqual([]);
    expect(settled.queuedPrompts).toBe(0);
    expect(fixture.transactions).toHaveLength(0);
  });
});
