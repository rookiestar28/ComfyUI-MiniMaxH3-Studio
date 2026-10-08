// M25-16: NLE workspace performance budgets measured at the ACTUAL integrated
// H3Sidebar/shell boundary (frontend/e2e/nleShell.tsx mounts the real presentationBinding
// EntrySidebar fragment via a real createShellSession/createNleWorkspaceSession), not only at
// the bespoke harness-parent component nleWorkspace.spec.ts measures. See
// frontend/e2e/nleShell.tsx for the two sibling Profilers and the authoring-action wire
// translator that reuses the accepted scripts/m25_16_timeline_fixture.py oracle.
import { test, expect, type Page, type Request } from "@playwright/test";
import { writeFile } from "node:fs/promises";

import { openExportPanel } from "../helpers/nleExport";
import {
  openIntegratedShell,
  shellMetadata as metadata,
  shellSnapshot as snapshot,
  shellSurface as surface,
} from "../helpers/nleShell";
import {
  playheadFrame,
  playheadSlider,
  seekPlayheadFraction,
} from "../helpers/nleTimeline";

const p95 = (values: readonly number[]): number => {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.ceil(sorted.length * 0.95) - 1]!;
};

const PLAYBACK_FPS = 24;

async function heapReader(page: Page) {
  const context = page.context();
  const cdp = await context.newCDPSession(page);
  await cdp.send("Performance.enable");
  return {
    read: async () => {
      await cdp.send("HeapProfiler.collectGarbage");
      const metrics = await cdp.send("Performance.getMetrics");
      const value = metrics.metrics.find(
        (entry: { name: string }) => entry.name === "JSHeapUsedSize",
      )?.value;
      if (typeof value !== "number" || !Number.isFinite(value))
        throw new Error("heap_instrumentation_unavailable");
      return value;
    },
    readListeners: async () => {
      const observed = await cdp.send("Runtime.evaluate", {
        expression: `(() => {
          const targets = [
            window,
            document,
            ...document.querySelectorAll('[data-h3-nle-surface="overlay_v1"],video,audio,canvas'),
          ];
          return targets.reduce((total, target) => {
            const groups = getEventListeners(target);
            return total + Object.values(groups).reduce(
              (sum, entries) => sum + entries.length,
              0,
            );
          }, 0);
        })()`,
        includeCommandLineAPI: true,
        returnByValue: true,
      });
      const value = observed.result.value;
      if (typeof value !== "number" || !Number.isSafeInteger(value))
        throw new Error("listener_instrumentation_unavailable");
      return value;
    },
    detach: () => cdp.detach(),
  };
}

function requestTracker(page: Page) {
  let active = 0;
  let peak = 0;
  let total = 0;
  const started = (_request: Request) => {
    active += 1;
    total += 1;
    peak = Math.max(peak, active);
  };
  const settled = (_request: Request) => {
    active = Math.max(0, active - 1);
  };
  page.on("request", started);
  page.on("requestfinished", settled);
  page.on("requestfailed", settled);
  return {
    read: () => ({ active, peak, total }),
    detach: () => {
      page.off("request", started);
      page.off("requestfinished", settled);
      page.off("requestfailed", settled);
    },
  };
}

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

test("counts Sidebar renders even when the same commit also renders the NLE", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await page.evaluate(() => {
    window.nleShellHarness.resetMeasurements();
    window.nleShellHarness.refreshView();
  });
  await expect
    .poll(async () => (await snapshot(page)).nleCommits)
    .toBeGreaterThan(0);
  // A refresh touches both siblings. Subtracting commits shared with the overlay hides
  // exactly the whole-Sidebar regression the continuous-playback budget must detect.
  expect((await snapshot(page)).parentCommits).toBeGreaterThan(0);
});

for (const shape of ["continuous-preview"] as const) {
  test(`${shape} integrated shell five-minute decoded playback keeps the Sidebar at zero commits per tick and within heap and render budgets`, async ({
    page,
  }, testInfo) => {
    test.setTimeout(480_000);
    // Repeated edit/undo during the five-minute interval is intentionally stateful. Use the
    // canonical persistent oracle (2,048 bounded transactions); replay mode's 32-row ceiling is
    // for short journeys and would stop this acceptance exercise before its resource assertions.
    await openIntegratedShell(page, shape, { service: true });
    let receipts = (await snapshot(page)).receipts;
    await page
      .locator(
        '[data-h3-nle-clip="preview-primary-0"] [data-h3-nle-control="selection.set"]',
      )
      .click();
    await expect
      .poll(async () => (await snapshot(page)).receipts)
      .toBe(receipts + 1);
    receipts += 1;
    const heap = await heapReader(page);
    const baseline = await heap.read();
    const baselineListeners = await heap.readListeners();
    await page.evaluate(() => window.nleShellHarness.resetMeasurements());
    const play = page.getByRole("button", { name: "Play", exact: true });
    const slider = playheadSlider(page);
    const durationFrames = Number(
      (await snapshot(page)).timelineSnapshot!.output.durationFrames,
    );
    expect(durationFrames).toBeGreaterThanOrEqual(240);
    const requests = requestTracker(page);
    let intervalStartFrame = await playheadFrame(slider);
    expect(intervalStartFrame).toBe(0);
    await play.click();
    await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
      "Monitor playing.",
    );
    let playedFrames = 0;
    let intervalPlayedFrames = 0;
    let replays = 0;
    let edits = 0;
    let undos = 0;
    let navigationActions = 0;
    const playbackIntervalsMs: number[] = [];
    const editSidebarCommitDeltas: number[] = [];
    const durationInvariant: number[] = [durationFrames];
    const resourceSamples: Array<{
      listeners: number;
      requests: ReturnType<typeof requests.read>;
      owners: Awaited<ReturnType<typeof snapshot>>["mediaOwnership"];
    }> = [];
    const maxima = { rows: 0, clips: 0, dom: 0 };
    while ((playedFrames * 1_000) / PLAYBACK_FPS < 300_000) {
      const previousFrame = await playheadFrame(slider);
      await page.waitForTimeout(3000);
      const stillPlaying =
        (await page.locator('[data-h3-nle-status="monitor"]').textContent()) ===
        "Monitor playing.";
      const nextFrame = await playheadFrame(slider);
      // IMPORTANT: derive the exercise duration from frames the native clock actually advanced.
      // Wall time after the last presented frame is an ended tail, not decoded playback; counting
      // the full polling interval here would let idle time satisfy the five-minute requirement.
      const advanced = Math.max(0, nextFrame - previousFrame);
      playedFrames += advanced;
      intervalPlayedFrames += advanced;
      if (stillPlaying) {
        expect(nextFrame).toBeGreaterThan(previousFrame);
      } else {
        // IMPORTANT: the five-minute native-clock budget must use real decoded source coverage.
        // Sparse geometry-only fixtures stop at their first source gap and cannot qualify replay.
        expect(nextFrame).toBe(durationFrames - 1);
        const intervalFrames = nextFrame - intervalStartFrame + 1;
        const intervalMs = (intervalFrames * 1_000) / PLAYBACK_FPS;
        expect(intervalMs).toBeGreaterThanOrEqual(10_000);
        // The UI can advance between Play and the first polling sample. Reaching the exact
        // endpoint proves those leading frames decoded; add only that measured prefix, never the
        // idle tail after playback stopped.
        playedFrames += Math.max(0, intervalFrames - intervalPlayedFrames);
        playbackIntervalsMs.push(intervalMs);
        intervalPlayedFrames = 0;
        await slider.focus();
        await page.keyboard.press("Home");
        navigationActions += 1;
        await expect(slider).toHaveAttribute("aria-valuenow", "0");
        await expect(slider).toHaveAttribute("aria-valuetext", "00:00:00:00");
        await page.keyboard.press("ArrowRight");
        navigationActions += 1;
        await expect(slider).toHaveAttribute("aria-valuenow", "1");
        await page.keyboard.press("Home");
        navigationActions += 1;
        await expect(slider).toHaveAttribute("aria-valuenow", "0");

        const intervalNumber = playbackIntervalsMs.length;
        if (intervalNumber === 1 || intervalNumber % 4 === 0) {
          const sidebarBeforeEdit = (await snapshot(page)).sidebarCommits;
          const inspector = page.locator(
            '[data-h3-nle-selected-clip="preview-primary-0"]',
          );
          const position = inspector.getByRole("spinbutton", {
            name: "Position X (%)",
            exact: true,
          });
          await position.fill(edits % 2 === 0 ? "1" : "-1");
          await inspector
            .locator('[data-h3-nle-control="visual.transform"]')
            .click();
          await expect
            .poll(async () => (await snapshot(page)).receipts)
            .toBe(receipts + 1);
          receipts += 1;
          edits += 1;
          durationInvariant.push(
            Number(
              (await snapshot(page)).timelineSnapshot!.output.durationFrames,
            ),
          );
          await page.locator('[data-h3-nle-control="history.undo"]').click();
          await expect
            .poll(async () => (await snapshot(page)).receipts)
            .toBe(receipts + 1);
          receipts += 1;
          undos += 1;
          durationInvariant.push(
            Number(
              (await snapshot(page)).timelineSnapshot!.output.durationFrames,
            ),
          );
          editSidebarCommitDeltas.push(
            (await snapshot(page)).sidebarCommits - sidebarBeforeEdit,
          );
        }
        await expect(play).toBeEnabled();
        intervalStartFrame = await playheadFrame(slider);
        expect(intervalStartFrame).toBe(0);
        await play.click();
        replays += 1;
        await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
          "Monitor playing.",
        );
      }
      if (!stillPlaying) {
        // IMPORTANT: retain one complete resource/DOM sample per >=10-second interval, but do not
        // run the heavyweight harness snapshot every three seconds while measuring presentation.
        // That observer pause lands on the native delivery path and would measure itself.
        const resourceSnapshot = await snapshot(page);
        resourceSamples.push({
          listeners: await heap.readListeners(),
          requests: requests.read(),
          owners: resourceSnapshot.mediaOwnership,
        });
        const counts = await page.locator(surface).evaluate((root) => ({
          rows: root.querySelectorAll("[data-h3-nle-track]").length,
          clips: root.querySelectorAll("[data-h3-nle-clip]").length,
          dom: root.querySelectorAll("*").length,
        }));
        for (const key of ["rows", "clips", "dom"] as const)
          maxima[key] = Math.max(maxima[key], counts[key]);
      }
    }
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await expect.poll(() => requests.read().active).toBe(0);
    const playedMs = (playedFrames * 1_000) / PLAYBACK_FPS;
    const growth = (await heap.read()) - baseline;
    const measurements = await snapshot(page);
    const meta = await metadata(page);
    const nleRenders = [...measurements.commitDurationsMs];
    // Fail closed: instrumentation absence must not read as a passing empty sample.
    if (nleRenders.length === 0)
      throw new Error("nle-overlay Profiler produced zero samples");
    const renderP95 = p95(nleRenders);
    // AC45-04: the whole compositor, from the render call to the frame after it. The React commit
    // above is a different quantity -- it is what the overlay's tree cost, not what the viewer
    // waited for -- and the larger preview is a claim about the second one.
    const delivered = measurements.presentationSamples.map(
      (sample) => sample.deliveredMs,
    );
    if (delivered.length === 0)
      throw new Error(
        "the composition session produced zero presentation samples",
      );
    if (measurements.presentationSamplesDropped !== 0)
      throw new Error(
        "presentation samples were dropped; the budget would read a partial run",
      );
    const presentationP95 = p95(delivered);
    const presentationMax = Math.max(...delivered);
    const presentationOutliers = measurements.presentationSamples.filter(
      (sample) => sample.deliveredMs > 50,
    );
    await page.locator('[data-h3-nle-action="close"]').click();
    await expect
      .poll(async () => (await snapshot(page)).mediaOwnership.live)
      .toBe(0);
    await expect.poll(() => requests.read().active).toBe(0);
    const closedMeasurements = await snapshot(page);
    const closedListeners = await heap.readListeners();
    const attributedSidebarCommits = editSidebarCommitDeltas.reduce(
      (total, commits) => total + commits,
      0,
    );
    const unattributedSidebarCommits =
      measurements.sidebarCommits - attributedSidebarCommits;
    const report = {
      shape,
      playedMs,
      heapGrowthBytes: growth,
      renderP95Ms: renderP95,
      presentationP95Ms: presentationP95,
      presentationMaxMs: presentationMax,
      presentationSamples: delivered.length,
      presentationOutliers,
      presentationPaintP95Ms: p95(
        measurements.presentationSamples.map((sample) => sample.paintMs),
      ),
      presentationPaths: [
        ...new Set(
          measurements.presentationSamples.map((sample) => sample.path),
        ),
      ],
      presentationBacking: [
        ...new Set(
          measurements.presentationSamples.map(
            (sample) => `${sample.backingWidth}x${sample.backingHeight}`,
          ),
        ),
      ],
      nleCommits: measurements.nleCommits,
      sidebarCommits: measurements.sidebarCommits,
      editSidebarCommitDeltas,
      attributedSidebarCommits,
      unattributedSidebarCommits,
      maxima,
      owners: measurements.mediaOwnership,
      closedOwners: closedMeasurements.mediaOwnership,
      resources: {
        baselineListeners,
        closedListeners,
        listenerSamples: resourceSamples.map((sample) => sample.listeners),
        requests: requests.read(),
        requestSamples: resourceSamples.map((sample) => sample.requests),
        ownerSamples: resourceSamples.map((sample) => sample.owners),
        replays,
        playbackIntervalsMs,
        minimumPlaybackIntervalMs: Math.min(...playbackIntervalsMs),
        edits,
        undos,
        navigationActions,
        durationInvariant,
      },
      gcPolicy:
        "CDP HeapProfiler.collectGarbage immediately before both heap samples",
      metadata: meta,
    };
    console.log(
      JSON.stringify({
        shape: report.shape,
        playedMs: report.playedMs,
        playbackIntervals: report.resources.playbackIntervalsMs.length,
        minimumPlaybackIntervalMs: report.resources.minimumPlaybackIntervalMs,
        edits: report.resources.edits,
        undos: report.resources.undos,
        navigationActions: report.resources.navigationActions,
        heapGrowthBytes: report.heapGrowthBytes,
        renderP95Ms: report.renderP95Ms,
        presentationP95Ms: report.presentationP95Ms,
        presentationMaxMs: report.presentationMaxMs,
        sidebarCommits: report.sidebarCommits,
        editSidebarCommitDeltas: report.editSidebarCommitDeltas,
        unattributedSidebarCommits: report.unattributedSidebarCommits,
        closedOwners: report.closedOwners,
        closedRequests: report.resources.requests,
        closedListeners: report.resources.closedListeners,
      }),
    );
    const reportPath = testInfo.outputPath(
      "integrated-five-minute-budgets.json",
    );
    await writeFile(reportPath, JSON.stringify(report, null, 2));
    await testInfo.attach("integrated-five-minute-budgets", {
      path: reportPath,
      contentType: "application/json",
    });
    // The Sidebar Profiler excludes the sibling overlay by construction. Attribute only commits
    // observed inside accepted edit/undo transactions; playback itself must still add none.
    expect(editSidebarCommitDeltas).toHaveLength(edits);
    expect(editSidebarCommitDeltas.every((commits) => commits === 4)).toBe(
      true,
    );
    expect(unattributedSidebarCommits).toBe(0);
    expect(growth).toBeLessThanOrEqual(128 * 1024 * 1024);
    expect(renderP95).toBeLessThanOrEqual(16);
    expect(presentationP95).toBeLessThanOrEqual(50);
    expect(presentationMax).toBeLessThanOrEqual(150);
    expect(maxima.rows).toBeLessThanOrEqual(8);
    expect(maxima.clips).toBeLessThanOrEqual(96);
    expect(maxima.dom).toBeLessThanOrEqual(1500);
    expect(playedMs).toBeGreaterThanOrEqual(300_000);
    expect(playbackIntervalsMs.length).toBeGreaterThanOrEqual(2);
    expect(Math.min(...playbackIntervalsMs)).toBeGreaterThanOrEqual(10_000);
    expect(edits).toBeGreaterThanOrEqual(8);
    expect(edits).toBeLessThan(playbackIntervalsMs.length);
    expect(undos).toBe(edits);
    expect(navigationActions).toBe(playbackIntervalsMs.length * 3);
    expect(durationInvariant.every((frames) => frames === durationFrames)).toBe(
      true,
    );
    expect(resourceSamples.length).toBe(playbackIntervalsMs.length);
    const firstListeners = Math.max(
      baselineListeners,
      ...resourceSamples.slice(0, 10).map((sample) => sample.listeners),
    );
    const lastListeners = Math.max(
      ...resourceSamples.slice(-10).map((sample) => sample.listeners),
    );
    expect(lastListeners).toBeLessThanOrEqual(firstListeners + 2);
    expect(requests.read().peak).toBeLessThanOrEqual(8);
    expect(requests.read().total).toBeLessThanOrEqual(replays * 8 + 8);
    for (const sample of resourceSamples) {
      expect(sample.owners.live).toBeLessThanOrEqual(8);
      expect(sample.owners.sourceLive).toBeLessThanOrEqual(8);
      expect(sample.owners.acquisitionsInFlight).toBeLessThanOrEqual(8);
      expect(sample.owners.retainedBytes).toBeLessThanOrEqual(
        128 * 1024 * 1024,
      );
      expect(sample.owners.blobUrls).toBeLessThanOrEqual(2);
      expect(sample.owners.videoElements).toBeLessThanOrEqual(2);
      expect(sample.owners.waveformBuffers).toBeLessThanOrEqual(2);
    }
    expect(closedMeasurements.mediaOwnership.sourceLive).toBe(0);
    expect(closedMeasurements.mediaOwnership.acquisitionsInFlight).toBe(0);
    expect(closedMeasurements.mediaOwnership.retainedBytes).toBe(0);
    expect(closedMeasurements.mediaOwnership.blobUrls).toBe(0);
    expect(closedMeasurements.mediaOwnership.videoElements).toBe(0);
    expect(closedMeasurements.mediaOwnership.waveformBuffers).toBe(0);
    expect(closedMeasurements.mediaOwnership.acquired).toBe(
      closedMeasurements.mediaOwnership.released,
    );
    expect(requests.read().active).toBe(0);
    expect(closedListeners).toBeLessThanOrEqual(baselineListeners + 2);
    requests.detach();
    await heap.detach();
  });
}

for (const shape of ["smoke", "virtualized"] as const) {
  test(`${shape} integrated shell discrete decoded edit stays within four NLE commits and handler budget`, async ({
    page,
  }, testInfo) => {
    await openIntegratedShell(page, shape);
    // Selection/setup is a real transaction too (it dispatches select_clips), but is counted and
    // reported separately from the discrete-edit budget below.
    await page.evaluate(() => window.nleShellHarness.resetMeasurements());
    await page
      .locator(
        '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
      )
      .click();
    await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
    const setupSnapshot = await snapshot(page);
    const setupCommits = setupSnapshot.nleCommits;

    const editCommits: number[] = [];
    const editRenders: number[] = [];
    const editHandlers: number[] = [];
    const editStates: string[] = [];
    const seriesStarted = await page.evaluate(() => performance.now());
    for (let index = 0; index < 10; index++) {
      await expect(
        page.getByRole("button", { name: "Play", exact: true }),
      ).toBeEnabled();
      await page.waitForTimeout(100);
      const inspector = page.locator('[data-h3-nle-selected-clip="clip-0"]');
      const input = inspector.getByRole("spinbutton", {
        name: "Position X (%)",
        exact: true,
      });
      // M25-64 (R10): 1 % is the 100 bp this series has always alternated with 0.
      await input.fill(index % 2 === 0 ? "1" : "0");
      await page.waitForTimeout(100);
      await page.evaluate(() => window.nleShellHarness.resetMeasurements());
      await inspector
        .locator('[data-h3-nle-control="visual.transform"]')
        .click();
      // Waits through accepted projection AND settled monitor state: the receipt count only
      // advances once the transaction is accepted and the follow-up history read has landed.
      await expect
        .poll(async () => (await snapshot(page)).receipts)
        .toBe(index + 2);
      await expect(
        page.getByRole("button", { name: "Play", exact: true }),
      ).toBeEnabled();
      await page.waitForTimeout(100);
      const measurements = await snapshot(page);
      editCommits.push(measurements.nleCommits);
      editRenders.push(...measurements.commitDurationsMs);
      editStates.push(...measurements.commitStates);
      const clickHandlers = measurements.handlerSamples.filter(
        (sample) =>
          sample.eventType === "click" && sample.control === "visual.transform",
      );
      expect(clickHandlers).toHaveLength(1);
      editHandlers.push(...clickHandlers.map((sample) => sample.durationMs));
    }
    const meta = await metadata(page);
    if (editRenders.length === 0)
      throw new Error("nle-overlay Profiler produced zero edit samples");
    if (editHandlers.length === 0)
      throw new Error("capture-to-bubble handler timing produced zero samples");
    const report = {
      shape,
      setupCommits,
      editCommits,
      renderP95Ms: p95(editRenders),
      handlerP95Ms: p95(editHandlers),
      renderSamplesSortedMs: [...editRenders]
        .sort((a, b) => a - b)
        .map((value) => Math.round(value * 10) / 10),
      renderMedianMs:
        ([...editRenders].sort((a, b) => a - b)[
          Math.floor((editRenders.length - 1) / 2)
        ]! +
          [...editRenders].sort((a, b) => a - b)[
            Math.floor(editRenders.length / 2)
          ]!) /
        2,
      handlerSamplesSortedMs: [...editHandlers]
        .sort((a, b) => a - b)
        .map((value) => Math.round(value * 10) / 10),
      slowCommitPhases: editRenders
        .map((duration, index) => ({ duration, state: editStates[index] }))
        .sort((a, b) => b.duration - a.duration)
        .slice(0, 3)
        .map(({ state }) => state?.split(":")[0] ?? "unknown"),
      sampleCounts: {
        renders: editRenders.length,
        handlers: editHandlers.length,
      },
      measuredIntervalMs:
        (await page.evaluate(() => performance.now())) - seriesStarted,
      lastInteractionMetadata: meta,
    };
    console.log(JSON.stringify(report));
    await testInfo.attach("integrated-discrete-edit-budgets", {
      body: JSON.stringify(report),
      contentType: "application/json",
    });
    expect(Math.max(...editCommits)).toBeLessThanOrEqual(4);
    expect(p95(editRenders)).toBeLessThanOrEqual(16);
    expect(p95(editHandlers)).toBeLessThanOrEqual(50);
  });
}

// Alternate distant ruler targets keep every pointer sample observable even after the prior seek.
const SEEK_POINTER_FRACTIONS = [
  0.25, 0.75, 0.3, 0.8, 0.35, 0.85, 0.4, 0.9, 0.2, 0.7,
];

for (const shape of ["smoke", "virtualized"] as const) {
  test(`${shape} integrated shell pointer and keyboard seek handlers meet the latency budget`, async ({
    page,
  }, testInfo) => {
    await openIntegratedShell(page, shape);
    const slider = playheadSlider(page);
    const samples: Record<string, number[]> = { pointer: [], keyboard: [] };
    const seriesStarted = await page.evaluate(() => performance.now());
    for (const via of ["pointer", "keyboard"] as const) {
      for (let index = 0; index < 10; index++) {
        const before = await playheadFrame(slider);
        await page.evaluate(() => window.nleShellHarness.resetMeasurements());
        if (via === "keyboard") {
          await slider.focus();
          await page.keyboard.press("ArrowRight");
        } else {
          await seekPlayheadFraction(
            page,
            slider,
            SEEK_POINTER_FRACTIONS[index]!,
          );
        }
        await expect.poll(() => playheadFrame(slider)).not.toBe(before);
        await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
          "Monitor paused.",
        );
        const seekEvent = via === "keyboard" ? "keydown" : "pointerdown";
        const handlers = (await snapshot(page)).handlerSamples.filter(
          (sample) =>
            sample.eventType === seekEvent &&
            sample.control === "transport.seek",
        );
        expect(handlers.length).toBeGreaterThan(0);
        samples[via]!.push(...handlers.map((sample) => sample.durationMs));
      }
    }
    const report = {
      shape,
      samples: {
        pointer: samples.pointer!.length,
        keyboard: samples.keyboard!.length,
      },
      pointerP95Ms: p95(samples.pointer!),
      keyboardP95Ms: p95(samples.keyboard!),
      measuredIntervalMs:
        (await page.evaluate(() => performance.now())) - seriesStarted,
      lastInteractionMetadata: await metadata(page),
    };
    console.log(JSON.stringify(report));
    await testInfo.attach("integrated-seek-handler-budgets", {
      body: JSON.stringify(report),
      contentType: "application/json",
    });
    expect(report.pointerP95Ms).toBeLessThanOrEqual(50);
    expect(report.keyboardP95Ms).toBeLessThanOrEqual(50);
  });

  test(`${shape} integrated shell releases every owner across close and remount`, async ({
    page,
  }) => {
    await openIntegratedShell(page, shape);
    const play = page.getByRole("button", { name: "Play", exact: true });
    const pause = page.getByRole("button", { name: "Pause", exact: true });
    await play.click();
    await expect
      .poll(async () => Number(await playheadFrame(playheadSlider(page))))
      .toBeGreaterThan(0);
    await page.waitForTimeout(700);
    await pause.click();
    await page.locator('[data-h3-nle-action="close"]').click();
    await expect
      .poll(async () => (await snapshot(page)).mediaOwnership.live)
      .toBe(0);
    const afterFirstClose = (await snapshot(page)).mediaOwnership;
    expect(afterFirstClose.acquired).toBe(afterFirstClose.released);
    expect(afterFirstClose.acquired).toBeGreaterThan(0);

    await page.getByRole("button", { name: "Open full editor" }).click();
    await expect(play).toBeEnabled();
    await play.click();
    await expect
      .poll(async () => Number(await playheadFrame(playheadSlider(page))))
      .toBeGreaterThan(0);
    await page.waitForTimeout(700);
    await pause.click();
    await page.locator('[data-h3-nle-action="close"]').click();
    await expect
      .poll(async () => (await snapshot(page)).mediaOwnership.live)
      .toBe(0);
    const final = (await snapshot(page)).mediaOwnership;
    expect(final.acquired).toBe(final.released);
    // No growth across generations: the second open/close cycle acquires and releases the same
    // bounded owner set as the first, never a higher peak.
    expect(final.maximumLive).toBe(afterFirstClose.maximumLive);
  });
}

test("integrated shell render affordance returns after close and remount while the first capability read is still pending", async ({
  page,
}) => {
  // Corrective F2: generation 1's capability read is held open; the overlay is closed and
  // reopened before it settles. Generation 2 must obtain its own capability (the affordance
  // mounts again) and the late generation-1 completion must neither clear nor replace it.
  // No render job is ever requested: the affordance only became interactive.
  await openIntegratedShell(page, "smoke", {
    render: true,
    capabilityDelayMs: 3_000,
  });
  const region = page.getByRole("region", { name: "Final video" });
  // The render card lives in the Export popover (M25-44); with it open, an absent region means
  // the capability read has not answered.
  await openExportPanel(page);
  await expect(region).toHaveCount(0);
  await page.locator('[data-h3-nle-action="close"]').click();
  await expect
    .poll(async () => (await snapshot(page)).surfaceStatus)
    .not.toBe("expanded");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(
    page.getByRole("button", { name: "Play", exact: true }),
  ).toBeEnabled();
  await openExportPanel(page);
  await expect(region).toBeVisible({ timeout: 10_000 });
  await expect(
    page.getByRole("button", { name: "Render final video" }),
  ).toBeEnabled();
  // Let the first generation's held reply land, then confirm it changed nothing.
  await page.waitForTimeout(3_500);
  await expect(region).toBeVisible();
  const after = await snapshot(page);
  expect(after.renderStatus).toBe("read");
  expect(after.renderSupported).toBe(true);
  expect(after.capabilityReads).toBe(2);
  expect(after.renderJobRequests).toBe(0);
  expect(after.generation).toBe(2);
});

for (const shape of ["smoke", "virtualized"] as const) {
  test(`${shape} integrated shell bounds rendered rows, clips and DOM while zooming and scrolling`, async ({
    page,
  }) => {
    await openIntegratedShell(page, shape);
    await page.locator('[data-h3-nle-control="transport.zoom_out"]').click();
    const scroll = page.locator('[data-h3-nle-control="transport.scroll"]');
    await scroll.focus();
    await page.keyboard.press("End");
    const counts = await page.locator(surface).evaluate((root) => ({
      rows: root.querySelectorAll("[data-h3-nle-track]").length,
      clips: root.querySelectorAll("[data-h3-nle-clip]").length,
      dom: root.querySelectorAll("*").length,
    }));
    expect(counts.rows).toBeGreaterThan(0);
    expect(counts.rows).toBeLessThanOrEqual(8);
    expect(counts.clips).toBeLessThanOrEqual(96);
    expect(counts.dom).toBeLessThanOrEqual(1500);
  });
}
