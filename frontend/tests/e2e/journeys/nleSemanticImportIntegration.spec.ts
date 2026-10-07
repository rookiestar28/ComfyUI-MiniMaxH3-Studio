// M25-20 F1 corrective, browser half: the two `import_integration` corpus rows
// (`generated_source.explicit_import_then_insert.pointer` / `.keyboard`,
// `comfyui_h3_context/core/semantic_conformance_cases.py`'s `IMPORT_INTEGRATION_CASE`). Each row
// performs a real explicit import through the product's own affordance (the Production stage's
// import control), by pointer for one row and by keyboard for the other, then a SEPARATE real
// insertion into the timeline, and counts the two effects separately rather than as one combined
// total -- exactly the AC10 shape the post-closeout review found only enumerated, never executed.
//
// This reuses the accepted M25-16/M25-29 import journey infrastructure
// (`frontend/e2e/nleShell.tsx`'s `import=1` mode, `scripts/m25_16_import_fixture.py` via
// `startImportFixture`) that `nleProductionImport.spec.ts` already exercises for AC11; this file
// adds the two named, per-case-id observations that corpus admission needs, it does not replace
// that journey's own broader coverage.
//
// M25-20 B-65: the fixture runs with `importMedia=corpus`, so the ready output the shell imports
// is the real encoded imported source the render stage also imports, probed by the real adapter;
// after the insertion the journey undoes it across the pre-import history and redoes it, through
// the shell's own history controls by the same gesture, and records the chain's invariant
// identities (`IMPORT_IDENTITY_FACTS` in `comfyui_h3_context/core/semantic_conformance.py`) so the
// join can hold this half to the render stage's. The monitor canvas is measured for its backing
// size because the composition is the product's fixed 1920x1080 timeline, which the preview
// scales down: the join then judges the artifact and records that this side presented it.

import { createHash } from "node:crypto";

import { expect, test, type Page } from "@playwright/test";

import {
  expectImportBootstrap,
  startImportFixture,
  type ImportFixture,
} from "../helpers/nleImportFixture";
import {
  recordShellObservation,
  timelineContent,
} from "../helpers/nleSemanticShellEvidence";
import { shellSnapshot, shellSurface } from "../helpers/nleShell";
import { MEDIA_RUNTIME_STATUS_ROUTE } from "../../../src/host/mediaRuntimeClient";

const AUTHORING_ROUTE = "/h3-context/v1/authoring/action";
const PRODUCTION_ROUTE = "/h3-context/v1/production/action";
const IMPORT_ROUTE = "/h3-context/v1/production/authoring-import";
const CAPABILITY_ROUTE = "/h3-context/v1/authoring/output-capability";
const importButton = '[data-h3-nle-control="asset.import_production"]';
const undoButton = '[data-h3-nle-control="history.undo"]';
const redoButton = '[data-h3-nle-control="history.redo"]';
// The monitor's own canvas, by its role marker: since M25-49 the media bin's card art canvases
// precede the monitor in document order, so the first canvas under the overlay surface is a
// zero-backing thumbnail, not the presented composition.
const MONITOR_CANVAS =
  '[data-h3-nle-surface="overlay_v1"] canvas[data-h3-nle-canvas="composition"]';

type Gesture = "pointer" | "keyboard";

async function activate(page: Page, selector: string, gesture: Gesture) {
  const button = page.locator(selector);
  await expect(button).toBeEnabled();
  if (gesture === "pointer") await button.click();
  else {
    await button.focus();
    await page.keyboard.press("Enter");
  }
}

/** The digest `scripts/nle_semantic_import_scenario.py`'s `landmark_table_sha256` computes. */
function landmarkTableSha256(
  landmarks: readonly Readonly<{ frameIndex: number; pts: number }>[],
): string {
  return createHash("sha256")
    .update(JSON.stringify(landmarks.map((row) => [row.frameIndex, row.pts])))
    .digest("hex");
}

/** The decoded snapshot types its output profile as a record; read one numeric field. */
function outputNumber(
  output: Readonly<Record<string, unknown>> | undefined,
  key: string,
): number | null {
  const value = output?.[key];
  return typeof value === "number" ? value : null;
}

async function canvasBacking(
  page: Page,
): Promise<Readonly<{ width: number; height: number }> | null> {
  const canvas = page.locator(MONITOR_CANVAS).first();
  if ((await canvas.count()) === 0) return null;
  return canvas.evaluate((element) => {
    const target = element as HTMLCanvasElement;
    return { width: target.width, height: target.height };
  });
}

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

async function openImportShell(page: Page): Promise<ImportFixture> {
  const fixture = await startImportFixture(page);
  // An already-initialized target: the import then reuses it (`read_timeline_history` only)
  // rather than also creating and initializing one, so the timeline revision genuinely stays
  // put across the import step and "not itself a timeline edit" is an observation, not an
  // artifact of there being no timeline snapshot to compare yet.
  await page.goto("/nleShell.html?import=1&target=ready&importMedia=corpus");
  try {
    // The corpus mode encodes and probes a real source; the wait is that mode's own budget.
    await expectImportBootstrap(fixture, 180_000);
  } catch (error) {
    await fixture.close();
    throw error;
  }
  await productionStage(page);
  await mediaStage(page);
  await expect(page.locator(importButton)).toBeEnabled();
  return fixture;
}

async function productionStage(page: Page): Promise<void> {
  await page
    .getByRole("navigation", { name: "H3 Context pages" })
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Production" }).click();
}

async function clipEditorStage(page: Page): Promise<void> {
  await page
    .getByRole("navigation", { name: "H3 Context pages" })
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Clip editor" }).click();
}

async function openOverlay(page: Page): Promise<void> {
  if (await page.locator(shellSurface).isVisible()) return;
  await page.locator('[data-h3-nle-entry="open"]').click();
  await expect(page.locator(shellSurface)).toBeVisible();
}

/** The Production-import control is owned by the expanded editor's Media tab. */
async function mediaStage(page: Page): Promise<void> {
  if (!(await page.locator(shellSurface).isVisible())) {
    await clipEditorStage(page);
    await openOverlay(page);
  }
  await page.locator('[data-h3-nle-pane="assets"]').click();
}

function foreignPaths(paths: readonly string[], since: number): string[] {
  return paths.slice(since).filter(
    (path) =>
      path !== AUTHORING_ROUTE &&
      path !== PRODUCTION_ROUTE &&
      path !== IMPORT_ROUTE &&
      path !== CAPABILITY_ROUTE &&
      // The overlay's Media tools card (M25-33) reads the runtime status to report its features.
      // IMPORTANT: admit exactly this read-only path; the setup route and every other
      // media-runtime path stay foreign.
      path !== MEDIA_RUNTIME_STATUS_ROUTE,
  );
}

async function expectImportSucceeded(page: Page): Promise<void> {
  await expect
    .poll(async () => (await shellSnapshot(page)).importStatus)
    .toBe("succeeded");
}

/**
 * Runs the shared explicit-import-then-insert journey for one input gesture, recording the
 * corpus row's observation with import and insertion effects counted separately, and asserting
 * that neither step is ever a prompt, queue or render-job request.
 */
async function runImportThenInsert(
  page: Page,
  testInfo: import("@playwright/test").TestInfo,
  gesture: Gesture,
): Promise<void> {
  const forbidden: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      request.method() === "POST" &&
      (path === "/prompt" || path === "/queue")
    )
      forbidden.push(path);
  });

  const fixture = await openImportShell(page);
  try {
    const beforeImport = await shellSnapshot(page);
    const beforeAuthoring = beforeImport.authoringStateV2;
    if (beforeAuthoring === null)
      throw new Error(
        "the initialized authoring session did not expose its V2 state",
      );

    // Step 1: the explicit import, through the product's own control, by the named gesture.
    if (gesture === "pointer") {
      await page.locator(importButton).click();
    } else {
      await page.locator(importButton).focus();
      await page.keyboard.press("Enter");
    }
    await expectImportSucceeded(page);
    // IMPORTANT (B-M2563-01): the import settles as `succeeded` first and the editor then re-reads
    // canonical history (nleWorkspaceImport.ts), so the workspace revision advances one read later.
    // Wait for that read; a refresh that never lands still fails here.
    await expect
      .poll(
        async () =>
          (await shellSnapshot(page)).authoringStateV2?.workspaceRevision ?? 0,
      )
      .toBeGreaterThan(beforeAuthoring.workspaceRevision);
    const afterImport = await shellSnapshot(page);
    const afterImportAuthoring = afterImport.authoringStateV2;
    if (afterImportAuthoring === null)
      throw new Error(
        "the import refresh did not expose its V2 authoring state",
      );
    const importEffects = {
      authoringActions: [...fixture.authoringActions],
      importRequestCount: fixture.importRequests.length,
      timelineTransactionCount: fixture.transactions.length,
      highlightedAssetCount: afterImport.highlightedAssetIds.length,
      timelineRevisionUnchanged:
        afterImportAuthoring.timelineRevision ===
        beforeAuthoring.timelineRevision,
      timelineFingerprintUnchanged:
        afterImportAuthoring.timelineFingerprint ===
        beforeAuthoring.timelineFingerprint,
      workspaceRevisionAdvanced:
        afterImportAuthoring.workspaceRevision >
        beforeAuthoring.workspaceRevision,
      queuedPrompts: afterImport.queuedPrompts,
      renderJobRequests: afterImport.renderJobRequests,
    };
    expect(importEffects.importRequestCount).toBe(1);
    expect(importEffects.timelineTransactionCount).toBe(0);
    expect(importEffects.timelineRevisionUnchanged).toBe(true);
    expect(importEffects.timelineFingerprintUnchanged).toBe(true);
    expect(importEffects.workspaceRevisionAdvanced).toBe(true);
    expect(importEffects.queuedPrompts).toBe(0);
    expect(importEffects.renderJobRequests).toBe(0);

    // Step 2: a SEPARATE, later insertion of the imported asset into the timeline, through the
    // product's own insert control on the opened overlay, by the same named gesture.
    const assetId = afterImport.highlightedAssetIds[0]!;
    await openOverlay(page);
    const importedCard = page.locator(
      `[data-h3-nle-region="asset-bin"] [data-h3-nle-asset="${assetId}"]`,
    );
    await expect(importedCard).toBeVisible();
    const insertControl = importedCard.getByRole("button", {
      name: /^Add .+ to the timeline$/,
    });
    await expect(insertControl).toBeEnabled();
    const beforeInsert = await shellSnapshot(page);
    if (gesture === "pointer") {
      await insertControl.click();
    } else {
      await insertControl.focus();
      await page.keyboard.press("Enter");
    }
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(beforeInsert.receipts + 1);
    const afterInsert = await shellSnapshot(page);
    const afterInsertAuthoring = afterInsert.authoringStateV2;
    if (afterInsertAuthoring === null)
      throw new Error(
        "the insertion receipt did not expose its V2 authoring state",
      );
    const insertionEffects = {
      timelineTransactionCount: fixture.transactions.length,
      insertedClipCount:
        afterInsertAuthoring.clips.filter((clip) => clip.assetId === assetId)
          .length ?? 0,
      timelineRevisionAdvanced:
        afterInsertAuthoring.timelineRevision >
        beforeAuthoring.timelineRevision,
      importRequestCountUnchanged:
        fixture.importRequests.length === importEffects.importRequestCount,
      queuedPrompts: afterInsert.queuedPrompts,
      renderJobRequests: afterInsert.renderJobRequests,
    };
    expect(insertionEffects.timelineTransactionCount).toBe(1);
    expect(insertionEffects.insertedClipCount).toBe(1);
    expect(insertionEffects.timelineRevisionAdvanced).toBe(true);
    expect(insertionEffects.importRequestCountUnchanged).toBe(true);
    expect(insertionEffects.queuedPrompts).toBe(0);
    expect(insertionEffects.renderJobRequests).toBe(0);

    // The composition the product now holds, as the shell decoded it: the render stage judges
    // the same composition reached through the real registries, and the join holds the two to
    // each other on the identities below.
    const inserted = afterInsertAuthoring.clips.find(
      (clip) => clip.assetId === assetId,
    );
    const importedAsset = afterInsertAuthoring.assets.find(
      (asset) => asset.assetId === assetId,
    );
    const canvas = await canvasBacking(page);
    const missing: string[] = [];
    if (inserted === undefined)
      missing.push("inserted clip: not in the snapshot");
    if (importedAsset === undefined)
      missing.push("imported asset: not in the snapshot");
    if (afterInsert.timelineSnapshot === null)
      missing.push(
        "materialized render snapshot: unavailable after positive content extent",
      );
    if (canvas === null) missing.push("monitor canvas: not presented");
    const bootstrap = fixture.bootstrap();
    if (bootstrap === null || bootstrap.media !== "corpus")
      missing.push("import fixture: corpus media was not served");

    // Step 3: undo the insertion across the pre-import history, then redo it, through the
    // shell's own history controls, by the same gesture. Restoration is judged on the
    // composition's content (`timelineContent`), and the library must keep the admitted asset.
    await activate(page, undoButton, gesture);
    try {
      await expect
        .poll(async () => (await shellSnapshot(page)).receipts)
        .toBe(beforeInsert.receipts + 2);
      await expect
        .poll(async () => {
          const state = await shellSnapshot(page);
          return (
            state.authoringStatus === "ready" &&
            state.timelineHistoryRedoAvailable
          );
        })
        .toBe(true);
    } catch (error) {
      const rejected = await shellSnapshot(page);
      const rejectedTransaction = fixture.transactions.at(-1);
      const rejectedCommands = Array.isArray(rejectedTransaction?.commands)
        ? rejectedTransaction.commands
        : [];
      await testInfo.attach(`m25-57-${gesture}-undo-after`, {
        body: Buffer.from(
          JSON.stringify(
            {
              authoringStatus: rejected.authoringStatus,
              authoringReason: rejected.authoringReason,
              timelineRevision: rejected.authoringStateV2?.timelineRevision,
              workspaceRevision: rejected.authoringStateV2?.workspaceRevision,
              redoAvailable: rejected.timelineHistoryRedoAvailable,
              historyRejectionCode: rejected.timelineHistoryRejectionCode,
              receipts: rejected.receipts,
              transactionCount: fixture.transactions.length,
              authoringActions: fixture.authoringActions.slice(-6),
              lastCommandKinds: rejectedCommands.map((command) =>
                typeof command === "object" &&
                command !== null &&
                typeof command.kind === "string"
                  ? command.kind
                  : "unknown",
              ),
            },
            null,
            2,
          ),
        ),
        contentType: "application/json",
      });
      throw error;
    }
    const afterUndo = await shellSnapshot(page);
    await activate(page, redoButton, gesture);
    await expect
      .poll(async () => (await shellSnapshot(page)).receipts)
      .toBe(beforeInsert.receipts + 3);
    const afterRedo = await shellSnapshot(page);
    const afterUndoAuthoring = afterUndo.authoringStateV2;
    const afterRedoAuthoring = afterRedo.authoringStateV2;
    const beforeInsertAuthoring = beforeInsert.authoringStateV2;
    const undoRestoredPreInsertTimeline =
      afterUndoAuthoring !== null &&
      beforeInsertAuthoring !== null &&
      timelineContent(afterUndoAuthoring) ===
        timelineContent(beforeInsertAuthoring);
    const redoRestoredPostInsertTimeline =
      afterRedoAuthoring !== null &&
      afterInsertAuthoring !== null &&
      timelineContent(afterRedoAuthoring) ===
        timelineContent(afterInsertAuthoring);
    const libraryRetainedAfterUndo =
      afterUndoAuthoring?.assets.some((asset) => asset.assetId === assetId) ??
      false;
    expect(fixture.transactions.length).toBe(3);
    expect(afterUndoAuthoring?.clips.length).toBe(
      beforeInsertAuthoring?.clips.length,
    );
    expect(undoRestoredPreInsertTimeline).toBe(true);
    expect(libraryRetainedAfterUndo).toBe(true);
    expect(redoRestoredPostInsertTimeline).toBe(true);

    const noForeignRoutes = [
      ...foreignPaths(afterImport.fetchPaths, beforeImport.fetchPaths.length),
      ...foreignPaths(afterInsert.fetchPaths, afterImport.fetchPaths.length),
      ...foreignPaths(afterRedo.fetchPaths, afterInsert.fetchPaths.length),
    ];
    expect(noForeignRoutes).toEqual([]);
    expect(forbidden).toEqual([]);

    const output = afterInsert.timelineSnapshot?.output;
    await recordShellObservation(testInfo, {
      case_id: `import_integration.generated_source.explicit_import_then_insert.${gesture}`,
      executed: true,
      canvas_width: canvas?.width ?? 0,
      canvas_height: canvas?.height ?? 0,
      facts: {
        gesture,
        importRequestCount: importEffects.importRequestCount,
        importTimelineTransactionCount: importEffects.timelineTransactionCount,
        importTimelineRevisionUnchanged:
          importEffects.timelineRevisionUnchanged,
        importTimelineFingerprintUnchanged:
          importEffects.timelineFingerprintUnchanged,
        importWorkspaceRevisionAdvanced:
          importEffects.workspaceRevisionAdvanced,
        insertionTimelineTransactionCount:
          insertionEffects.timelineTransactionCount,
        insertedClipCount: insertionEffects.insertedClipCount,
        insertionTimelineRevisionAdvanced:
          insertionEffects.timelineRevisionAdvanced,
        importRequestCountUnchangedByInsertion:
          insertionEffects.importRequestCountUnchanged,
        queuedPrompts: afterRedo.queuedPrompts,
        renderJobRequests: afterRedo.renderJobRequests,
        forbiddenPromptOrQueuePosts: forbidden.length,
        foreignRoutePosts: noForeignRoutes.length,
        // The chain's identities, named as the render stage names them.
        source_fingerprint: bootstrap?.sourceFingerprint ?? null,
        imported_source_frame_count: importedAsset?.sourceFrameCount ?? null,
        imported_source_landmark_table_sha256:
          importedAsset === undefined
            ? null
            : landmarkTableSha256(importedAsset.landmarks),
        receipt_rows: afterImport.importDispositions.length,
        import_changed_timeline_revision:
          !importEffects.timelineRevisionUnchanged,
        import_changed_timeline_fingerprint:
          !importEffects.timelineFingerprintUnchanged,
        import_advanced_workspace_revision:
          importEffects.workspaceRevisionAdvanced,
        pre_import_timeline_revision: beforeAuthoring.timelineRevision,
        post_import_timeline_revision: afterImportAuthoring.timelineRevision,
        post_insert_timeline_revision: afterInsertAuthoring.timelineRevision,
        post_undo_timeline_revision:
          afterUndoAuthoring?.timelineRevision ?? null,
        post_redo_timeline_revision:
          afterRedoAuthoring?.timelineRevision ?? null,
        inserted_clip_id: inserted?.clipId ?? null,
        inserted_clip_track_id: inserted?.trackId ?? null,
        inserted_clip_start_frame: inserted?.startFrame ?? null,
        inserted_clip_duration_frames: inserted?.durationFrames ?? null,
        inserted_clip_source_start_frame: inserted?.sourceStartFrame ?? null,
        output_width: outputNumber(output, "width"),
        output_height: outputNumber(output, "height"),
        output_duration_frames: outputNumber(output, "durationFrames"),
        post_import_clip_count: afterImportAuthoring.clips.length,
        post_insert_clip_count: afterInsertAuthoring.clips.length,
        post_undo_clip_count: afterUndoAuthoring?.clips.length ?? null,
        post_redo_clip_count: afterRedoAuthoring?.clips.length ?? null,
        undo_restored_pre_insert_timeline: undoRestoredPreInsertTimeline,
        library_retained_after_undo: libraryRetainedAfterUndo,
        redo_restored_post_insert_timeline: redoRestoredPostInsertTimeline,
      },
      missing,
    });
  } finally {
    await fixture.close();
  }
}

test("pointer: explicit import then a separate insertion, effects counted separately", async ({
  page,
}, testInfo) => {
  await runImportThenInsert(page, testInfo, "pointer");
});

test("keyboard: explicit import then a separate insertion, effects counted separately", async ({
  page,
}, testInfo) => {
  await runImportThenInsert(page, testInfo, "keyboard");
});
