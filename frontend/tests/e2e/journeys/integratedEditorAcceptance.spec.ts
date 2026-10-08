import {
  expect,
  test,
  type Page,
  type Route,
  type TestInfo,
} from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { openExportPanel } from "../helpers/nleExport";
import {
  expectImportBootstrap,
  startImportFixture,
} from "../helpers/nleImportFixture";
import { loseCanvasContext } from "../helpers/nleMonitorPicture";
import { observeIntegratedOutput } from "../helpers/nleOutputObserver";
import { shellSnapshot, shellSurface } from "../helpers/nleShell";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";
import {
  audioPacketTimelineViolation,
  startProcessAudioObserver,
  type AudioCapture,
} from "../host/audioObserver";

const IMPORT = '[data-h3-nle-control="asset.import_production"]';
const MONITOR = '[data-h3-nle-status="monitor"]';
const AUDIO = '[data-h3-nle-status="audio"]';
const repositoryRoot = resolve(process.cwd(), "..");
const processAudioObserverPath = resolve(
  repositoryRoot,
  "scripts/process_audio_observer.py",
);
const processAudioObserverSha256 =
  // IMPORTANT: audio evidence is process-scoped only when this reviewed observer is pinned.
  // A drifted observer could capture outside the owned browser or retain raw audio.
  "b7a6eb2454d800fac0072ce445cf6f4a263aa7157cbb11207e5455be50ae815c"; // pragma: allowlist secret

test.use({
  viewport: { width: 1600, height: 900 },
  deviceScaleFactor: 1,
  launchOptions: { ignoreDefaultArgs: ["--mute-audio"] },
});

async function ownedBrowserPid(page: Page): Promise<number> {
  const browser = page.context().browser();
  if (!browser) throw new Error("owned Chromium browser required");
  const cdp = await browser.newBrowserCDPSession();
  try {
    const processInfo = await cdp.send("SystemInfo.getProcessInfo");
    const browserPid = processInfo.processInfo.find(
      (row) => row.type === "browser",
    )?.id;
    if (!Number.isInteger(browserPid) || !browserPid || browserPid < 1)
      throw new Error("owned browser process identity missing");
    return browserPid;
  } finally {
    await cdp.detach();
  }
}

function summarizeImportedPreviewAudio(
  capture: AudioCapture,
  expectedFrames: 248 | 240,
  cutFrame: 124 | 120,
) {
  const toneWindows = capture.metrics.envelopes.filter(
    (envelope) =>
      envelope.channel_rms.every((rms) => rms >= 0.01 && rms <= 0.2) &&
      envelope.channel_frequency_hz.every(
        (frequency) => Math.abs(frequency - 440) <= 5,
      ),
  );
  const firstToneSample = toneWindows[0]?.start_sample;
  const finalTone = toneWindows.at(-1);
  if (firstToneSample === undefined || finalTone === undefined)
    throw new Error("imported preview emitted no qualifying tone window");
  const lastToneSample = finalTone.start_sample + finalTone.sample_count;
  const expectedCutSample = firstToneSample + cutFrame * 2_000;
  const cutRegionStart = expectedCutSample - 12_000;
  const cutRegionEnd = expectedCutSample + 12_000;
  const cutRegionToneWindows = toneWindows.filter(
    (window) =>
      window.start_sample < cutRegionEnd &&
      window.start_sample + window.sample_count > cutRegionStart,
  );
  const interiorSilenceRuns = capture.metrics.silence_runs
    .filter(
      (run) =>
        run.start_sample < lastToneSample &&
        run.start_sample + run.duration_samples > firstToneSample,
    )
    .map((run) => {
      const startSample = Math.max(run.start_sample, firstToneSample);
      const endSample = Math.min(
        run.start_sample + run.duration_samples,
        lastToneSample,
      );
      return {
        channel: run.channel,
        startSample,
        durationSamples: endSample - startSample,
      };
    });
  const channelMedianFrequencyHz = ([0, 1] as const).map((channel) => {
    const values = toneWindows
      .map((window) => window.channel_frequency_hz[channel])
      .sort((left, right) => left - right);
    return values[Math.floor(values.length / 2)] ?? 0;
  });
  return {
    schema: "h3.context.integrated_editor_process_audio.v1",
    observerSha256: processAudioObserverSha256,
    browserExecutableSha256: capture.browserExecutableSha256,
    selector: capture.selector,
    sampleRate: capture.sampleRate,
    windowSamples: capture.metrics.window_samples,
    samplesAnalyzed: capture.metrics.samples_analyzed,
    packetTimeline: capture.packetTimeline,
    packetTimelineViolation: audioPacketTimelineViolation(capture),
    qualifyingToneWindows: toneWindows.length,
    toneSpanSamples: lastToneSample - firstToneSample,
    expectedPlaybackSamples: expectedFrames * 2_000,
    firstToneSample,
    lastToneSample,
    expectedCutSample,
    cutRegionSamples: [cutRegionStart, cutRegionEnd],
    cutRegionToneWindows: cutRegionToneWindows.length,
    maximumInteriorSilenceSamples: Math.max(
      0,
      ...interiorSilenceRuns.map((run) => run.durationSamples),
    ),
    interiorSilenceRuns: interiorSilenceRuns.slice(0, 16),
    channelMedianFrequencyHz,
    trailingSilenceSamples: ([0, 1] as const).map(
      (channel) =>
        capture.metrics.silence_runs.find(
          (run) =>
            run.channel === channel &&
            run.start_sample + run.duration_samples >=
              capture.metrics.samples_analyzed - 480,
        )?.duration_samples ?? 0,
    ),
    rawAudioRetained: capture.rawAudioRetained,
    microphoneOpened: capture.microphoneOpened,
  };
}

async function openPublicEditor(page: Page): Promise<void> {
  await page
    .getByRole("navigation", { name: "H3 Context pages" })
    .getByRole("button", { name: "Production" })
    .click();
  await page.getByRole("tab", { name: "Production", exact: true }).click();
  await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
  await page.locator('[data-h3-nle-entry="open"]').click();
  await expect(page.locator(shellSurface)).toBeVisible();
  await page.locator('[data-h3-nle-pane="assets"]').click();
}

async function waitForReceipt(page: Page, previous: number): Promise<number> {
  await expect
    .poll(async () => (await shellSnapshot(page)).receipts)
    .toBe(previous + 1);
  return previous + 1;
}

async function addAsset(
  page: Page,
  assetId: string,
  receipts: number,
): Promise<number> {
  const card = page.locator(`[data-h3-nle-asset="${assetId}"]`);
  await expect(card).toBeVisible();
  await card.locator('[data-h3-nle-control="asset.insert"]').click();
  return waitForReceipt(page, receipts);
}

async function selectClip(
  page: Page,
  clipId: string,
  receipts: number,
): Promise<number> {
  await page
    .locator(
      `[data-h3-nle-clip="${clipId}"] [data-h3-nle-control="selection.set"]`,
    )
    .click();
  return waitForReceipt(page, receipts);
}

async function expectExtent(
  page: Page,
  frames: number,
  clips: readonly (readonly [number, number])[],
): Promise<void> {
  await expect
    .poll(
      async () =>
        (await shellSnapshot(page)).authoringStateV2?.contentEndExclusive,
    )
    .toBe(frames);
  const current = await shellSnapshot(page);
  expect(current.timelineSnapshot?.output.durationFrames).toBe(frames);
  expect(
    current.timelineSnapshot?.clips.map(({ startFrame, durationFrames }) => [
      startFrame,
      durationFrames,
    ]),
  ).toEqual(clips);
  await expect(playheadSlider(page)).toHaveAttribute(
    "aria-valuemax",
    String(frames - 1),
  );
}

async function exerciseIntegratedVariant(
  page: Page,
  testInfo: TestInfo,
  imported: Awaited<ReturnType<typeof startImportFixture>>,
  label: "variant-124-plus-124" | "variant-120-plus-120",
  expectedFrames: 248 | 240,
  cutFrame: 124 | 120,
) {
  const timeline = page.locator('[data-h3-nle-region="timeline"]');
  await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
  await expect(timeline).toHaveAttribute("data-h3-nle-view-start", "0");
  const scaleBeforeZoom = Number(
    await timeline.getAttribute("data-h3-nle-pixels-per-frame"),
  );
  const grid = page.getByRole("grid", { name: "Timeline tracks" });
  const gridBox = await grid.boundingBox();
  if (gridBox === null) throw new Error("timeline grid is not rendered");
  await page.mouse.move(
    gridBox.x + gridBox.width * 0.65,
    gridBox.y + gridBox.height * 0.5,
  );
  await page.keyboard.down("Control");
  await page.mouse.wheel(0, -120);
  await page.keyboard.up("Control");
  await expect
    .poll(async () =>
      Number(await timeline.getAttribute("data-h3-nle-pixels-per-frame")),
    )
    .toBeGreaterThan(scaleBeforeZoom);

  const slider = playheadSlider(page);
  await seekPlayhead(page, slider, 48);
  await expect(page.locator(".h3-nle-playhead")).toBeVisible();
  await slider.focus();
  await page.keyboard.press("Home");
  await expect(slider).toHaveAttribute("aria-valuenow", "0");
  let processObserver:
    Awaited<ReturnType<typeof startProcessAudioObserver>> | undefined;
  let processAudio: ReturnType<typeof summarizeImportedPreviewAudio>;
  try {
    processObserver = await startProcessAudioObserver(
      repositoryRoot,
      await ownedBrowserPid(page),
      processAudioObserverPath,
      processAudioObserverSha256,
    );
    await page.locator('[data-h3-nle-control="transport.play"]').click();
    await expect(page.locator(MONITOR)).toHaveText("Monitor playing.");
    await expect(page.locator(AUDIO)).toHaveAttribute(
      "data-h3-nle-audio-state",
      "following",
    );
    await expect(page.locator(AUDIO)).toHaveAttribute(
      "data-h3-nle-audio-reason",
      "none",
    );
    await expect
      .poll(() => playheadFrame(slider), { timeout: 35_000 })
      .toBeGreaterThan(cutFrame);
    await expect(slider).toHaveAttribute(
      "aria-valuenow",
      String(expectedFrames - 1),
      { timeout: 35_000 },
    );
    await expect(page.locator(MONITOR)).toHaveText("Monitor paused.");
    await page.waitForTimeout(350);
    const capture = await processObserver.stop();
    processObserver = undefined;
    processAudio = summarizeImportedPreviewAudio(
      capture,
      expectedFrames,
      cutFrame,
    );
  } finally {
    await processObserver?.stop();
  }
  const processAudioPath = testInfo.outputPath(
    `${label}-imported-preview-process-audio.json`,
  );
  await writeFile(processAudioPath, JSON.stringify(processAudio, null, 2));
  await testInfo.attach(`${label}-imported-preview-process-audio`, {
    contentType: "application/json",
    path: processAudioPath,
  });
  expect(processAudio.rawAudioRetained).toBe(false);
  expect(processAudio.microphoneOpened).toBe(false);
  expect(processAudio.sampleRate).toBe(48_000);
  expect(processAudio.windowSamples).toBe(480);
  expect(processAudio.packetTimelineViolation).toBeNull();
  expect(processAudio.samplesAnalyzed).toBeGreaterThan(expectedFrames * 2_000);
  expect(processAudio.toneSpanSamples).toBeGreaterThanOrEqual(
    expectedFrames * 2_000 - 36_000,
  );
  expect(processAudio.cutRegionToneWindows).toBeGreaterThanOrEqual(20);
  expect(processAudio.maximumInteriorSilenceSamples).toBeLessThan(960);
  for (const value of processAudio.channelMedianFrequencyHz)
    expect(value).toBeCloseTo(440, 1);
  expect(
    processAudio.trailingSilenceSamples.every((value) => value >= 960),
  ).toBe(true);

  await slider.focus();
  await page.keyboard.press("Home");
  await page.locator('[data-h3-nle-control="transport.play"]').click();
  await expect.poll(() => playheadFrame(slider)).toBeGreaterThan(12);
  await page.locator('[data-h3-nle-control="transport.pause"]').click();
  const stoppedFrame = await playheadFrame(slider);
  await page.locator('[data-h3-nle-control="transport.play"]').click();
  await expect.poll(() => playheadFrame(slider)).toBeGreaterThan(stoppedFrame);
  await page.locator('[data-h3-nle-control="transport.pause"]').click();

  // Install and remove this route by identity. Removing the whole pattern would also remove the
  // accepted imported-media route and turn a recoverable source failure into a harness failure.
  const deadSource = async (route: Route) => route.abort();
  await page.route("**/nle-media/*", deadSource);
  try {
    await loseCanvasContext(page, true);
    await expect(page.locator(MONITOR)).toHaveText(
      "Monitor unavailable: canvas unavailable.",
    );
    await loseCanvasContext(page, false);
    await page.locator('[data-h3-nle-control="transport.recover"]').click();
    await expect(page.locator(MONITOR)).toHaveText(
      "Monitor unavailable: source unavailable.",
      { timeout: 20_000 },
    );
  } finally {
    await page.unroute("**/nle-media/*", deadSource);
  }
  await page.locator('[data-h3-nle-control="transport.recover"]').click();
  await expect(page.locator(MONITOR)).toHaveText("Monitor paused.", {
    timeout: 30_000,
  });

  await openExportPanel(page);
  const outputRegion = page.getByRole("region", {
    name: "Final video",
    exact: true,
  });
  const createBefore = imported.output.counts.create;
  await outputRegion
    .getByRole("button", { name: "Render final video", exact: true })
    .click();
  await expect.poll(() => imported.output.counts.create).toBe(createBefore + 1);
  await expect(outputRegion.getByRole("status").first()).toHaveText(
    "Video ready",
    { timeout: 180_000 },
  );
  await expect(outputRegion).toContainText(`${expectedFrames / 24}s`);

  const browserDownload = page.waitForEvent("download");
  await outputRegion
    .getByRole("link", { name: "Download original", exact: true })
    .click();
  const completedDownload = await browserDownload;
  expect(await completedDownload.failure()).toBeNull();
  const outputPath = testInfo.outputPath(`${label}-product-output.mp4`);
  await completedDownload.saveAs(outputPath);
  const productStatus = imported.output
    .statuses()
    .filter((status) => status.availability === "available")
    .at(-1);
  const productSummary = productStatus?.output as
    Record<string, unknown> | undefined;
  expect(productStatus?.phase).toBe("succeeded");
  expect(productSummary?.frame_count).toBe(expectedFrames);
  const observation = observeIntegratedOutput(
    outputPath,
    expectedFrames,
    cutFrame,
  );
  expect(observation.outputBytes).toBe(productSummary?.byte_length);
  expect(`sha256:${observation.outputSha256}`).toBe(
    productSummary?.output_fingerprint,
  );
  expect(observation.video.frames).toBe(expectedFrames);
  expect(observation.video.rate).toBe("24/1");
  expect(observation.video.durationSeconds).toBeCloseTo(expectedFrames / 24, 2);
  expect(observation.audio.streams).toBe(1);
  expect(observation.audio.decodedSamples).toBeGreaterThanOrEqual(
    expectedFrames * 2_000 - 1_024,
  );
  expect(observation.audio.maximumPacketGapMs).toBeLessThanOrEqual(25);
  expect(
    observation.audio.maximumSilentRunSamplesAroundCut,
  ).toBeLessThanOrEqual(1_024);
  for (const value of Object.values(observation.audio.rms))
    expect(value).toBeGreaterThan(0.01);
  const observationPath = testInfo.outputPath(
    `${label}-independent-output-observation.json`,
  );
  await writeFile(observationPath, JSON.stringify(observation, null, 2));
  await testInfo.attach(`${label}-independent-output-observation`, {
    contentType: "application/json",
    path: observationPath,
  });
  return {
    observation,
    processAudio,
    finalPlayheadFrame: await playheadFrame(slider),
    browserDownload: true,
  };
}

test("public editor completes the imported two-clip edit, recovery, and render journey", async ({
  page,
}, testInfo) => {
  test.skip(
    process.platform !== "win32",
    "integrated process audio capture is Windows-only",
  );
  test.setTimeout(420_000);
  const imported = await startImportFixture(page, { realOutput: true });
  try {
    await page.goto(
      "/nleShell.html?import=1&target=absent&segments=2&importMedia=m25_56_tone124&render=1&outputJob=1",
    );
    await expectImportBootstrap(imported);
    await openPublicEditor(page);
    const empty = await shellSnapshot(page);
    expect(empty.authoringWorkspaceHandle).toBeNull();
    expect(empty.timelineSnapshot).toBeNull();
    await expect(playheadSlider(page)).toHaveCount(0);

    await expect(page.locator(IMPORT)).toBeEnabled();
    await page.locator(IMPORT).click();
    await expect
      .poll(async () => (await shellSnapshot(page)).importStatus)
      .toBe("succeeded");
    const afterImport = await shellSnapshot(page);
    expect(afterImport.highlightedAssetIds).toHaveLength(2);
    expect(afterImport.authoringStateV2?.contentEndExclusive).toBe(0);
    expect(afterImport.authoringStateV2?.clips).toEqual([]);
    expect(imported.importRequests).toHaveLength(1);
    const workspaceHandle = afterImport.authoringWorkspaceHandle;
    expect(workspaceHandle).not.toBeNull();
    const [firstAsset, secondAsset] = afterImport.highlightedAssetIds;
    let receipts = afterImport.receipts;

    // First prove both unmodified 124-frame imported sources through the two public Add controls.
    receipts = await addAsset(page, firstAsset!, receipts);
    receipts = await addAsset(page, secondAsset!, receipts);
    await expectExtent(page, 248, [
      [0, 124],
      [124, 124],
    ]);
    const variant124 = (await shellSnapshot(page)).timelineSnapshot!.clips.map(
      ({ startFrame, durationFrames }) => [startFrame, durationFrames],
    );
    const accepted124 = await exerciseIntegratedVariant(
      page,
      testInfo,
      imported,
      "variant-124-plus-124",
      248,
      124,
    );

    // Return to the same imported empty workspace, then use the same two Add controls and public
    // trim-at-playhead commands to establish the exact 120 + 120 edit variant.
    await page.locator('[data-h3-nle-control="history.undo"]').click();
    receipts = await waitForReceipt(page, receipts);
    await page.locator('[data-h3-nle-control="history.undo"]').click();
    receipts = await waitForReceipt(page, receipts);
    await expect
      .poll(
        async () =>
          (await shellSnapshot(page)).authoringStateV2?.contentEndExclusive,
      )
      .toBe(0);
    await expect(playheadSlider(page)).toHaveCount(0);

    receipts = await addAsset(page, firstAsset!, receipts);
    let clips = (await shellSnapshot(page)).timelineSnapshot!.clips;
    receipts = await selectClip(page, clips[0]!.clipId, receipts);
    await seekPlayhead(page, playheadSlider(page), 120);
    await page
      .locator('[data-h3-nle-control="clip.trim_end_playhead"]')
      .click();
    receipts = await waitForReceipt(page, receipts);
    await expectExtent(page, 120, [[0, 120]]);

    receipts = await addAsset(page, secondAsset!, receipts);
    // IMPORTANT: the receipt counter commits before React republishes the derived timeline
    // snapshot. Reading immediately can observe the transient null projection and turn a valid
    // transaction into a harness crash; the public extent is the stable publication boundary.
    await expectExtent(page, 244, [
      [0, 120],
      [120, 124],
    ]);
    clips = (await shellSnapshot(page)).timelineSnapshot!.clips;
    receipts = await selectClip(page, clips[1]!.clipId, receipts);
    await seekPlayhead(page, playheadSlider(page), 240);
    await page
      .locator('[data-h3-nle-control="clip.trim_end_playhead"]')
      .click();
    receipts = await waitForReceipt(page, receipts);
    await expectExtent(page, 240, [
      [0, 120],
      [120, 120],
    ]);

    await page.locator('[data-h3-nle-control="history.undo"]').click();
    receipts = await waitForReceipt(page, receipts);
    await expectExtent(page, 244, [
      [0, 120],
      [120, 124],
    ]);
    await page.locator('[data-h3-nle-control="history.redo"]').click();
    receipts = await waitForReceipt(page, receipts);
    await expectExtent(page, 240, [
      [0, 120],
      [120, 120],
    ]);

    const accepted120 = await exerciseIntegratedVariant(
      page,
      testInfo,
      imported,
      "variant-120-plus-120",
      240,
      120,
    );

    const completed = await shellSnapshot(page);
    expect(completed.authoringWorkspaceHandle).toBe(workspaceHandle);
    expect(completed.authoringStateV2?.contentEndExclusive).toBe(240);
    expect(completed.queuedPrompts).toBe(0);
    expect(imported.importRequests).toHaveLength(1);
    expect(imported.output.counts.create).toBe(2);
    const acceptanceReport = {
      schema: "h3.context.integrated_editor_acceptance.v1",
      workspaceStable: completed.authoringWorkspaceHandle === workspaceHandle,
      imports: imported.importRequests.length,
      variant124,
      variant120: completed.timelineSnapshot!.clips.map(
        ({ startFrame, durationFrames }) => [startFrame, durationFrames],
      ),
      outputDurationFrames: completed.timelineSnapshot!.output.durationFrames,
      finalPlayheadFrame: accepted120.finalPlayheadFrame,
      renderCreates: imported.output.counts.create,
      renderDownloads:
        Number(accepted124.browserDownload) +
        Number(accepted120.browserDownload),
      outputObservations: [accepted124.observation, accepted120.observation],
      previewProcessAudio: [accepted124.processAudio, accepted120.processAudio],
      queuedPrompts: completed.queuedPrompts,
      mediaOwnership: completed.mediaOwnership,
    };
    await page.locator('[data-h3-nle-action="close"]').click();
    await expect(page.locator(shellSurface)).toHaveCount(0);
    await expect
      .poll(async () => (await shellSnapshot(page)).mediaOwnership.live)
      .toBe(0);
    const closed = (await shellSnapshot(page)).mediaOwnership;
    expect(closed.sourceLive).toBe(0);
    expect(closed.acquisitionsInFlight).toBe(0);
    expect(closed.acquired).toBe(closed.released);
    const acceptancePath = testInfo.outputPath(
      "integrated-editor-acceptance.json",
    );
    await writeFile(
      acceptancePath,
      JSON.stringify(
        { ...acceptanceReport, closedMediaOwnership: closed },
        null,
        2,
      ),
    );
    await testInfo.attach("integrated-editor-acceptance", {
      contentType: "application/json",
      path: acceptancePath,
    });
  } finally {
    await imported.close();
  }
});
