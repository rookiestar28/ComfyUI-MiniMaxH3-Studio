// The preview and the final render state one clip audio envelope. An imported tone is inserted
// twice; the second clip is set through the Audio tab to -6 dB with one-second fades. Its preview,
// captured from the owned browser process, and its final render, decoded with the authorized
// pair, are measured in the same 480-sample windows and compared with the definition and with
// each other, normalised by the first clip's steady level. A second preview pass with the clip
// muted proves no part of a muted owner is heard.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import semanticManifest from "../../../../governance/contracts/nle_semantic_conformance_manifest_v1.json" with { type: "json" };
import {
  IDENTITY_CLIP_AUDIO,
  type ClipAudio,
} from "../../../src/contracts/compositionCodec";
import { clipAudioFactor } from "../../../src/runtime/clipAudioEnvelope";
import { EXPORT_BUTTON, openExportPanel } from "../helpers/nleExport";
import {
  expectImportBootstrap,
  startImportFixture,
} from "../helpers/nleImportFixture";
import { shellSnapshot, shellSurface } from "../helpers/nleShell";
import { playheadSlider } from "../helpers/nleTimeline";
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

const CLIP_FRAMES = 124;
const SAMPLES_PER_FRAME = 2_000;
const CLIP_SAMPLES = CLIP_FRAMES * SAMPLES_PER_FRAME;
const TIMELINE_FRAMES = 2 * CLIP_FRAMES;
const WINDOW = 480;
const FADE_FRAMES = 24;
const FADE_SAMPLES = FADE_FRAMES * SAMPLES_PER_FRAME;
const ADJUSTED: ClipAudio = Object.freeze({
  gainMb: -600,
  muted: false,
  fadeInFrames: FADE_FRAMES,
  fadeOutFrames: FADE_FRAMES,
});
const MUTED: ClipAudio = Object.freeze({ ...ADJUSTED, muted: true });
// Clip-relative centres of the measured windows: 1/4, 1/2 and 3/4 of each fade and the middle.
const POINTS = Object.freeze([
  ["fade_in_1_4", FADE_SAMPLES / 4],
  ["fade_in_1_2", FADE_SAMPLES / 2],
  ["fade_in_3_4", (3 * FADE_SAMPLES) / 4],
  ["steady_middle", CLIP_SAMPLES / 2],
  ["fade_out_1_4", CLIP_SAMPLES - FADE_SAMPLES + FADE_SAMPLES / 4],
  ["fade_out_1_2", CLIP_SAMPLES - FADE_SAMPLES / 2],
  ["fade_out_3_4", CLIP_SAMPLES - FADE_SAMPLES / 4],
] as const);
// GUARD: in this harness the preview does not play the imported file. The shell's lease client
// answers every audio preview with `syntheticAudioPreview` (`frontend/e2e/nleWorkspaceMedia.ts`):
// a 440 Hz tone at 0.08 carrying a 24-sample onset marker at 0.8 on source samples
// [24000, 24024), which the M25-56 observer journeys locate. The final renders the imported file,
// which has no marker. Clip B plays its source from sample 0, so the marker sits at the middle of
// its 24-frame fade-in, and a window holding it measures the marker, not the envelope (0.83
// against a defined 0.25). Each preview window is therefore the capture's window nearest its
// point that excludes the marker, and the final is measured on exactly the samples the preview
// window covers; never measure the nominal window when it holds the marker.
const PREVIEW_SOURCE_MARKER = Object.freeze({ start: 24_000, end: 24_024 });
// The corpus's frozen audio-level bounds (plan 2.8): relative to the defined level, never below
// the floor, both in millionths of the reference level.
const TOLERANCE = semanticManifest.tolerance;
const RELATIVE = TOLERANCE.audio_level_relative_error_ppm / 1_000_000;
const FLOOR = TOLERANCE.audio_level_floor_ppm / 1_000_000;
const bound = (reference: number) => Math.max(FLOOR, RELATIVE * reference);

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

/** The defined level of a window: the RMS of the factor over its samples. */
function definedLevel(audio: ClipAudio, start: number): number {
  let sum = 0;
  for (let index = 0; index < WINDOW; index += 1) {
    const factor = clipAudioFactor(audio, CLIP_FRAMES, start + index);
    sum += factor * factor;
  }
  return Math.sqrt(sum / WINDOW);
}

const median = (values: readonly number[]) => {
  const sorted = [...values].sort((left, right) => left - right);
  if (sorted.length === 0) throw new Error("no steady window to normalise by");
  return sorted[Math.floor(sorted.length / 2)]!;
};

/** One level for a stereo window of the mono mix the browser plays. */
const stereoLevel = (rms: readonly [number, number]) =>
  Math.sqrt((rms[0] * rms[0] + rms[1] * rms[1]) / 2);

/**
 * Locates the second clip's first sample in the capture from the level step at the cut and
 * measures its windows. The first clip plays at the identity, so its steady level is the
 * reference; the window holding the step carries the first clip's energy for the part before the
 * cut, which places the cut inside that window (the second clip's own contribution there is at
 * most a factor of 0.005 when it fades in, and none when muted).
 */
function measurePreview(capture: AudioCapture, audio: ClipAudio) {
  const envelopes = capture.metrics.envelopes;
  const toneWindows = envelopes.filter(
    (envelope) =>
      envelope.channel_rms.every((rms) => rms >= 0.01 && rms <= 0.2) &&
      envelope.channel_frequency_hz.every(
        (frequency) => Math.abs(frequency - 440) <= 5,
      ),
  );
  const firstToneSample = toneWindows[0]?.start_sample;
  if (firstToneSample === undefined)
    throw new Error("the preview emitted no qualifying tone window");
  const nominalCut = firstToneSample + CLIP_SAMPLES;
  const reference = median(
    envelopes
      .filter(
        (envelope) =>
          envelope.start_sample >= firstToneSample + 24_000 &&
          envelope.start_sample + envelope.sample_count <= nominalCut - 24_000,
      )
      .map((envelope) => stereoLevel(envelope.channel_rms)),
  );
  const region = envelopes
    .map((envelope, index) => ({ envelope, index }))
    .filter(
      ({ envelope }) =>
        envelope.start_sample >= nominalCut - 24_000 &&
        envelope.start_sample <= nominalCut + 24_000,
    );
  const drop = region.find(
    ({ envelope }) => stereoLevel(envelope.channel_rms) < 0.5 * reference,
  );
  if (drop === undefined)
    throw new Error("the level step from the first clip was not found");
  const before = envelopes[drop.index - 1];
  const step =
    before !== undefined && stereoLevel(before.channel_rms) < 0.98 * reference
      ? before
      : drop.envelope;
  const firstClipShare = Math.min(
    1,
    (stereoLevel(step.channel_rms) / reference) ** 2,
  );
  const cut = step.start_sample + WINDOW * firstClipShare;
  const first = envelopes[0]!.start_sample;
  const clipStartOf = (index: number) =>
    Math.round(envelopes[index]!.start_sample - cut);
  const holdsMarker = (index: number) =>
    clipStartOf(index) < PREVIEW_SOURCE_MARKER.end &&
    clipStartOf(index) + WINDOW > PREVIEW_SOURCE_MARKER.start;
  const windows = POINTS.map(([name, centre]) => {
    const nominal = Math.round((cut + centre - WINDOW / 2 - first) / WINDOW);
    if (envelopes[nominal] === undefined)
      throw new Error(`the capture has no full window at ${name}`);
    // The nearer neighbour that excludes the marker, when the nominal window holds it.
    const index = !holdsMarker(nominal)
      ? nominal
      : [nominal - 1, nominal + 1]
          .filter(
            (candidate) =>
              envelopes[candidate] !== undefined && !holdsMarker(candidate),
          )
          .sort(
            (left, right) =>
              Math.abs(clipStartOf(left) + WINDOW / 2 - centre) -
              Math.abs(clipStartOf(right) + WINDOW / 2 - centre),
          )[0];
    const envelope = index === undefined ? undefined : envelopes[index];
    if (envelope === undefined || envelope.sample_count !== WINDOW)
      throw new Error(`the capture has no full window at ${name}`);
    const clipStart = Math.round(envelope.start_sample - cut);
    return {
      name,
      clipStartSample: clipStart,
      markerAvoided: index !== nominal,
      measured: stereoLevel(envelope.channel_rms) / reference,
      defined: definedLevel(audio, clipStart),
    };
  });
  // The first whole window after the cut: a muted owner heard for one render quantum (128
  // samples) would lift it far above the floor.
  const afterCut = envelopes.find((envelope) => envelope.start_sample >= cut);
  return {
    firstToneSample,
    nominalCutSample: nominalCut,
    cutSample: cut,
    cutDriftSamples: cut - nominalCut,
    referenceLevel: reference,
    windows,
    firstWindowAfterCut:
      afterCut === undefined
        ? null
        : {
            clipStartSample: Math.round(afterCut.start_sample - cut),
            measured: stereoLevel(afterCut.channel_rms) / reference,
            defined: definedLevel(
              audio,
              Math.round(afterCut.start_sample - cut),
            ),
          },
  };
}

function executable(variable: string): string {
  const value = process.env[variable];
  if (!value) throw new Error(`${variable} must name the authorized pair`);
  return value;
}

/**
 * The final render's audio, decoded by the authorized pair to 48 kHz mono, measured exactly on
 * the clip-relative windows the preview measured.
 */
function measureFinal(
  path: string,
  audio: ClipAudio,
  clipStarts: readonly number[],
) {
  const ffmpeg = executable("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH");
  const pcm = execFileSync(
    ffmpeg,
    [
      "-v",
      "error",
      "-i",
      path,
      "-map",
      "0:a:0",
      "-ac",
      "1",
      "-ar",
      "48000",
      "-f",
      "s16le",
      "-",
    ],
    { timeout: 120_000, maxBuffer: 2 * 1024 * 1024 },
  );
  const samples = new Int16Array(
    pcm.buffer,
    pcm.byteOffset,
    Math.floor(pcm.byteLength / Int16Array.BYTES_PER_ELEMENT),
  );
  const rms = (start: number, count: number) => {
    if (start < 0 || start + count > samples.length)
      throw new Error("a final window is outside the decoded audio");
    let sum = 0;
    for (let index = start; index < start + count; index += 1) {
      const value = samples[index]! / 32_768;
      sum += value * value;
    }
    return Math.sqrt(sum / count);
  };
  const reference = rms(24_000, CLIP_SAMPLES - 48_000);
  return {
    ffmpegSha256: createHash("sha256")
      .update(readFileSync(ffmpeg))
      .digest("hex"),
    decodedSamples: samples.length,
    referenceLevel: reference,
    windows: POINTS.map(([name], index) => {
      const clipStart = clipStarts[index]!;
      return {
        name,
        clipStartSample: clipStart,
        measured: rms(CLIP_SAMPLES + clipStart, WINDOW) / reference,
        defined: definedLevel(audio, clipStart),
      };
    }),
  };
}

async function waitForReceipt(page: Page, previous: number): Promise<number> {
  await expect
    .poll(async () => (await shellSnapshot(page)).receipts)
    .toBe(previous + 1);
  return previous + 1;
}

async function secondClipAudio(page: Page): Promise<ClipAudio | undefined> {
  return (await shellSnapshot(page)).timelineSnapshot?.clips[1]?.audio;
}

/** Plays the whole timeline from frame 0 under the process observer. */
async function capturePlayback(page: Page): Promise<AudioCapture> {
  const slider = playheadSlider(page);
  await slider.focus();
  await page.keyboard.press("Home");
  await expect(slider).toHaveAttribute("aria-valuenow", "0");
  let observer:
    Awaited<ReturnType<typeof startProcessAudioObserver>> | undefined;
  try {
    observer = await startProcessAudioObserver(
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
    await expect(slider).toHaveAttribute(
      "aria-valuenow",
      String(TIMELINE_FRAMES - 1),
      { timeout: 35_000 },
    );
    await expect(page.locator(MONITOR)).toHaveText("Monitor paused.");
    await page.waitForTimeout(350);
    const capture = await observer.stop();
    observer = undefined;
    expect(capture.rawAudioRetained).toBe(false);
    expect(capture.microphoneOpened).toBe(false);
    expect(capture.sampleRate).toBe(48_000);
    expect(capture.metrics.window_samples).toBe(WINDOW);
    expect(audioPacketTimelineViolation(capture)).toBeNull();
    return capture;
  } finally {
    await observer?.stop();
  }
}

test("M25-77 preview and final render state the same clip audio envelope", async ({
  page,
}, testInfo) => {
  test.skip(
    process.platform !== "win32",
    "WASAPI process loopback is Windows-only",
  );
  test.setTimeout(420_000);
  const imported = await startImportFixture(page, { realOutput: true });
  try {
    await page.goto(
      "/nleShell.html?import=1&target=absent&importMedia=m25_56_tone124&render=1&outputJob=1",
    );
    await expectImportBootstrap(imported);
    await page
      .getByRole("navigation", { name: "H3 Context pages" })
      .getByRole("button", { name: "Production" })
      .click();
    await page.getByRole("tab", { name: "Production", exact: true }).click();
    await page.getByRole("tab", { name: "Clip editor", exact: true }).click();
    await page.locator('[data-h3-nle-entry="open"]').click();
    await expect(page.locator(shellSurface)).toBeVisible();
    await page.locator('[data-h3-nle-pane="assets"]').click();
    await page.locator(IMPORT).click();
    await expect
      .poll(async () => (await shellSnapshot(page)).importStatus)
      .toBe("succeeded");
    const afterImport = await shellSnapshot(page);
    const assetId = afterImport.highlightedAssetIds[0];
    if (assetId === undefined)
      throw new Error("imported asset identity missing");
    let receipts = afterImport.receipts;

    // The same imported tone twice: clip A at the identity, clip B straight after it.
    const card = page.locator(`[data-h3-nle-asset="${assetId}"]`);
    for (let insert = 0; insert < 2; insert += 1) {
      await card.locator('[data-h3-nle-control="asset.insert"]').click();
      receipts = await waitForReceipt(page, receipts);
    }
    await expect
      .poll(async () =>
        (await shellSnapshot(page)).timelineSnapshot?.clips.map(
          ({ startFrame, durationFrames }) => [startFrame, durationFrames],
        ),
      )
      .toEqual([
        [0, CLIP_FRAMES],
        [CLIP_FRAMES, CLIP_FRAMES],
      ]);
    const clips = (await shellSnapshot(page)).timelineSnapshot!.clips;
    const clipB = clips[1]!.clipId;
    await page
      .locator(
        `[data-h3-nle-clip="${clipB}"] [data-h3-nle-control="selection.set"]`,
      )
      .click();
    receipts = await waitForReceipt(page, receipts);

    // The Audio tab: the whole draft is sent as one command by the group's commit button.
    await page.locator('[data-h3-nle-property-tab="audio"]').click();
    const inspector = page.locator(
      `${shellSurface} [data-h3-nle-area="inspector"]`,
    );
    await inspector
      .getByRole("spinbutton", { name: "Volume (dB)", exact: true })
      .fill("-6");
    await inspector
      .getByRole("spinbutton", { name: "Fade in (s)", exact: true })
      .fill("1");
    await inspector
      .getByRole("spinbutton", { name: "Fade out (s)", exact: true })
      .fill("1");
    await inspector.locator('[data-h3-nle-control="audio.clip"]').click();
    await expect.poll(() => secondClipAudio(page)).toEqual(ADJUSTED);
    expect(
      (await shellSnapshot(page)).timelineSnapshot!.clips[0]!.audio,
    ).toEqual(IDENTITY_CLIP_AUDIO);

    const previewCapture = await capturePlayback(page);
    const preview = measurePreview(previewCapture, ADJUSTED);

    await openExportPanel(page);
    const outputRegion = page.getByRole("region", {
      name: "Final video",
      exact: true,
    });
    const createBefore = imported.output.counts.create;
    await outputRegion
      .getByRole("button", { name: "Render final video", exact: true })
      .click();
    await expect
      .poll(() => imported.output.counts.create)
      .toBe(createBefore + 1);
    await expect(outputRegion.getByRole("status").first()).toHaveText(
      "Video ready",
      { timeout: 180_000 },
    );
    const browserDownload = page.waitForEvent("download");
    await outputRegion
      .getByRole("link", { name: "Download original", exact: true })
      .click();
    const download = await browserDownload;
    expect(await download.failure()).toBeNull();
    const outputPath = testInfo.outputPath("m25-77-parity-output.mp4");
    await download.saveAs(outputPath);
    const productStatus = imported.output
      .statuses()
      .filter((status) => status.availability === "available")
      .at(-1);
    const productSummary = productStatus?.output as
      Record<string, unknown> | undefined;
    expect(productStatus?.phase).toBe("succeeded");
    expect(productSummary?.frame_count).toBe(TIMELINE_FRAMES);
    const outputBytes = readFileSync(outputPath);
    expect(
      `sha256:${createHash("sha256").update(outputBytes).digest("hex")}`,
    ).toBe(productSummary?.output_fingerprint);
    const final = measureFinal(
      outputPath,
      ADJUSTED,
      preview.windows.map(({ clipStartSample }) => clipStartSample),
    );
    await page.locator(EXPORT_BUTTON).click();
    await expect(page.locator(EXPORT_BUTTON)).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    // The mute switch is its own release boundary: one command with the group's whole draft.
    await inspector.getByRole("switch", { name: "Mute", exact: true }).click();
    await expect.poll(() => secondClipAudio(page)).toEqual(MUTED);
    const mutedCapture = await capturePlayback(page);
    const muted = measurePreview(mutedCapture, MUTED);

    const windows = preview.windows.map((window, index) => {
      const rendered = final.windows[index]!;
      return {
        name: window.name,
        definedPreview: window.defined,
        definedFinal: rendered.defined,
        preview: window.measured,
        final: rendered.measured,
        previewMinusDefinition: window.measured - window.defined,
        finalMinusDefinition: rendered.measured - rendered.defined,
        previewMinusFinal: window.measured - rendered.measured,
        previewBound: bound(window.defined),
        finalBound: bound(rendered.defined),
        previewFinalBound: bound(rendered.measured),
        previewClipStartSample: window.clipStartSample,
        finalClipStartSample: rendered.clipStartSample,
        markerAvoided: window.markerAvoided,
      };
    });
    const mutedWindows = [
      ...muted.windows,
      ...(muted.firstWindowAfterCut === null
        ? []
        : [{ name: "first_after_cut", ...muted.firstWindowAfterCut }]),
    ].map((window) => ({
      name: window.name,
      clipStartSample: window.clipStartSample,
      measured: window.measured,
      defined: window.defined,
      bound: bound(window.defined),
    }));
    const evidence = {
      schema: "h3.context.m25_77_clip_audio_parity.v1",
      observerSha256: processAudioObserverSha256,
      browserExecutableSha256: previewCapture.browserExecutableSha256,
      selector: previewCapture.selector,
      ffmpegSha256: final.ffmpegSha256,
      outputFingerprint: productSummary?.output_fingerprint,
      outputBytes: outputBytes.byteLength,
      clip: { frames: CLIP_FRAMES, audio: ADJUSTED },
      previewSourceMarker: PREVIEW_SOURCE_MARKER,
      tolerance: {
        version: TOLERANCE.version,
        relativePpm: TOLERANCE.audio_level_relative_error_ppm,
        floorPpm: TOLERANCE.audio_level_floor_ppm,
      },
      // The window resolution bounds the alignment error at 1 % of a 24-frame ramp.
      alignment: {
        windowSamples: WINDOW,
        rampSamples: FADE_SAMPLES,
        boundFraction: WINDOW / FADE_SAMPLES,
        previewCutSample: preview.cutSample,
        previewCutDriftSamples: preview.cutDriftSamples,
        mutedCutDriftSamples: muted.cutDriftSamples,
      },
      referenceLevels: {
        preview: preview.referenceLevel,
        final: final.referenceLevel,
        muted: muted.referenceLevel,
      },
      windows,
      mutedWindows,
      rawAudioRetained:
        previewCapture.rawAudioRetained || mutedCapture.rawAudioRetained,
      microphoneOpened:
        previewCapture.microphoneOpened || mutedCapture.microphoneOpened,
    };
    const evidencePath = testInfo.outputPath("m25-77-clip-audio-parity.json");
    await writeFile(evidencePath, JSON.stringify(evidence, null, 2));
    await testInfo.attach("m25-77-clip-audio-parity", {
      contentType: "application/json",
      path: evidencePath,
    });
    console.log(`M25-77_CLIP_AUDIO_PARITY=${JSON.stringify(evidence)}`);

    expect(Math.abs(preview.cutDriftSamples)).toBeLessThan(FADE_SAMPLES / 4);
    for (const window of windows) {
      expect
        .soft(
          Math.abs(window.previewMinusDefinition),
          `${window.name}: preview vs definition`,
        )
        .toBeLessThanOrEqual(window.previewBound);
      expect
        .soft(
          Math.abs(window.finalMinusDefinition),
          `${window.name}: final vs definition`,
        )
        .toBeLessThanOrEqual(window.finalBound);
      expect
        .soft(
          Math.abs(window.previewMinusFinal),
          `${window.name}: preview vs final`,
        )
        .toBeLessThanOrEqual(window.previewFinalBound);
    }
    for (const window of mutedWindows)
      expect
        .soft(
          Math.abs(window.measured - window.defined),
          `${window.name}: muted preview`,
        )
        .toBeLessThanOrEqual(window.bound);
  } finally {
    await imported.close();
  }
});
