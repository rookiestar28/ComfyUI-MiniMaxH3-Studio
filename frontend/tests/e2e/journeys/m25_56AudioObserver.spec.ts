import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type TestInfo,
} from "@playwright/test";

import parity from "../../../../tests/fixtures/m25_16_scene_resolver_parity_v1.json" with { type: "json" };
import {
  decodePublicCompositionSnapshot,
  type PublicCompositionSnapshot,
} from "../../../src/contracts/compositionCodec";
import { installHeardCutCoordinatorPrototype } from "../../prototypes/heardCutCoordinatorPrototype";
import { PtsSceneClockPrototype } from "../../prototypes/m25_56PtsSceneClockPrototype";
import {
  expectImportBootstrap,
  startImportFixture,
} from "../helpers/nleImportFixture";
import { shellLastPresentation, shellSnapshot } from "../helpers/nleShell";
import {
  audioPacketTimelineViolation,
  startProcessAudioObserver,
  type AudioCapture,
} from "../host/audioObserver";

const repositoryRoot = resolve(process.cwd(), "..");
const processAudioObserverPath = resolve(
  repositoryRoot,
  "scripts/process_audio_observer.py",
);
const processAudioObserverSha256 =
  // IMPORTANT: keep this exact content pin aligned with the reviewed observer; mismatch must fail
  // before process launch so an unreviewed process cannot capture the test browser's audio.
  "39923ffe21e1c19d78d61f167941cd65f181f0f4e86f4acbaa972d3983aff9f2"; // pragma: allowlist secret
const audioContextPcmFixturePath = resolve(
  repositoryRoot,
  "tests/fixtures/m25_56_audio_48khz_stereo_pcm.wav",
);
const audioContextPcmFixtureSha256 = createHash("sha256")
  .update(readFileSync(audioContextPcmFixturePath))
  .digest("hex");
if (
  audioContextPcmFixtureSha256 !==
  "6031c674d3a012510e4f0ab6864e0b66c06f443bc11d20258185245d4b1bb9f7" // pragma: allowlist secret
)
  throw new Error("M25-56 AudioContext PCM fixture hash mismatch");
const primaryVideoFixturePath = resolve(
  repositoryRoot,
  "tests/fixtures/m25_56_primary_motion_124f.mp4",
);
const primaryVideoFixtureSha256 = createHash("sha256")
  .update(readFileSync(primaryVideoFixturePath))
  .digest("hex");
if (
  primaryVideoFixtureSha256 !==
  "619bcdaa3f3d553efa8e20903cde47f101accd374dc1fae0b4845105a76d21f9" // pragma: allowlist secret
)
  throw new Error("M25-56 primary AudioContext video fixture hash mismatch");
const secondaryAudioFixturePath = resolve(
  repositoryRoot,
  "tests/fixtures/m25_56_secondary_48khz_stereo_pcm.wav",
);
const secondaryAudioFixtureSha256 = createHash("sha256")
  .update(readFileSync(secondaryAudioFixturePath))
  .digest("hex");
if (
  secondaryAudioFixtureSha256 !==
  "bb6b35adc8a2e2cfa5b70428cd1c7d4afe7faf374d116a13574bb61ee71b918a" // pragma: allowlist secret
)
  throw new Error("M25-56 secondary AudioContext PCM fixture hash mismatch");
const secondaryVideoSourceFixturePath = resolve(
  repositoryRoot,
  "tests/fixtures/m25_56_secondary_motion_124f.mp4",
);
const secondaryVideoSourceFixtureSha256 = createHash("sha256")
  .update(readFileSync(secondaryVideoSourceFixturePath))
  .digest("hex");
if (
  secondaryVideoSourceFixtureSha256 !==
  "b93b4f1ba6dc048231d570486389d70436d05d2affb6a27c4fb708836a182159" // pragma: allowlist secret
)
  throw new Error("M25-56 secondary source video fixture hash mismatch");
const secondaryVideoFixturePath = resolve(
  repositoryRoot,
  "tests/fixtures/m25_56_secondary_motion_from_frame12_112f.mp4",
);
const secondaryVideoFixtureSha256 = createHash("sha256")
  .update(readFileSync(secondaryVideoFixturePath))
  .digest("hex");
if (
  secondaryVideoFixtureSha256 !==
  "97f2a4ae80c0359afe9b3426893705adf67a419915d42aa802308a2328f0cce5" // pragma: allowlist secret
)
  throw new Error("M25-56 secondary AudioContext video fixture hash mismatch");
const splitCase = (
  parity as {
    cases: readonly Readonly<{
      name: string;
      snapshot: Record<string, unknown>;
    }>[];
  }
).cases.find((candidate) => candidate.name === "primary_split_two_owners");
if (splitCase === undefined)
  throw new Error("primary split fixture is missing");
const splitSnapshot = decodePublicCompositionSnapshot(splitCase.snapshot);
function denseExactCfrAsset(
  asset: PublicCompositionSnapshot["assets"][number],
) {
  const first = asset.landmarks[0];
  const second = asset.landmarks[1];
  if (
    first === undefined ||
    second === undefined ||
    asset.sourceFrameCount === null
  )
    throw new Error(`CFR landmark table is incomplete for ${asset.assetId}`);
  const frameDelta = second.frameIndex - first.frameIndex;
  const tickDelta = second.pts - first.pts;
  if (frameDelta <= 0 || tickDelta <= 0 || tickDelta % frameDelta !== 0)
    throw new Error(`CFR landmark table is not integral for ${asset.assetId}`);
  const ticksPerFrame = tickDelta / frameDelta;
  if (
    asset.landmarks.some(
      (landmark) =>
        landmark.pts !==
        first.pts + (landmark.frameIndex - first.frameIndex) * ticksPerFrame,
    )
  )
    throw new Error(`CFR landmarks disagree for ${asset.assetId}`);
  return {
    ...asset,
    landmarks: Array.from(
      { length: asset.sourceFrameCount },
      (_, frameIndex) => ({
        frameIndex,
        pts: first.pts + (frameIndex - first.frameIndex) * ticksPerFrame,
        dts: first.dts + (frameIndex - first.frameIndex) * ticksPerFrame,
        durationTicks: ticksPerFrame,
      }),
    ),
  };
}
const splitSnapshotWithSecondOwner = (() => {
  const primary = splitSnapshot.assets.find(
    (candidate) => candidate.assetId === "vid-primary",
  );
  if (primary === undefined) throw new Error("primary asset is missing");
  const expandedPrimary = denseExactCfrAsset(primary);
  return {
    ...splitSnapshot,
    assets: [
      ...splitSnapshot.assets.map((asset) =>
        asset.assetId === primary.assetId ? expandedPrimary : asset,
      ),
      { ...expandedPrimary, assetId: "vid-secondary" },
    ],
    clips: splitSnapshot.clips.map((clip) =>
      clip.clipId === "clip-main-right"
        ? { ...clip, assetId: "vid-secondary" }
        : clip,
    ),
  } as PublicCompositionSnapshot;
})();
const admittedSecondarySourceInPoint = (() => {
  const clip = splitSnapshotWithSecondOwner.clips.find(
    (candidate) => candidate.clipId === "clip-main-right",
  );
  const asset = splitSnapshotWithSecondOwner.assets.find(
    (candidate) => candidate.assetId === "vid-secondary",
  );
  const landmark = asset?.landmarks.find(
    (candidate) => candidate.frameIndex === clip?.sourceStartFrame,
  );
  if (
    clip === undefined ||
    asset === undefined ||
    asset.sourceTimeBase === null ||
    landmark === undefined
  )
    throw new Error("secondary admitted source in-point is unavailable");
  return {
    pts: landmark.pts,
    seconds:
      (landmark.pts * asset.sourceTimeBase.num) / asset.sourceTimeBase.den,
  };
})();

type NativePtsClockProbe = Readonly<{
  observations: readonly Readonly<{
    ownerId: string;
    assetId: string;
    epoch: number;
    mediaTime: number;
    presentedFrames: number;
    callbackTime: number;
  }>[];
  playCalls: number;
  pauseCalls: number;
  seekingEvents: number;
  warmupPlayCalls: number;
  warmupPauseCalls: number;
  warmupSeekingEvents: number;
  warmupComplete: boolean;
  started: boolean;
  callbacksAfterStop: number;
  cut: Readonly<{
    primaryMediaTime: number;
    secondaryMediaTime: number;
    synchronizationDifferenceMs: number;
    activeAudibleOwners: number;
  }> | null;
  stopped: boolean;
}>;

type AudioCutClockDiagnostics = Readonly<{
  fault: string;
  totalClockReads: number;
  lastAdvancePerformanceTime: number | null;
  samples: readonly Readonly<
    Record<string, number | string | boolean | null>
  >[];
  events: readonly Readonly<Record<string, number | string | boolean | null>>[];
  keyEvents: Readonly<
    Record<string, Readonly<Record<string, number | string | boolean | null>>>
  >;
  firstInvalidClock: Readonly<
    Record<string, number | string | boolean | null>
  > | null;
}>;

type AudioContextBufferProbe = Readonly<{
  ownedResources?: Readonly<{
    callbackCount: number;
    timerCount: number;
    sourceNodeCount: number;
    retainedBufferBytes: number;
  }>;
  protocolRefusal?: Readonly<{
    reason: string;
    performanceTime: number;
  }> | null;
  heardCutProtocol?: Readonly<{
    status: string;
    reason: string | null;
    released: boolean;
    releaseContextTime: number | null;
    refusedAtPerformanceTime: number | null;
    timerActive: boolean;
  }> | null;
  nativeHeldDecoder?: Readonly<{
    mediaTime: number;
    currentTime: number;
    confirmedPerformanceTime: number;
    paused: boolean;
    playbackRate: number;
  }> | null;
  clockDiagnostics?: AudioCutClockDiagnostics;
  prewarmReadyStates?: readonly number[];
  sourceAssets?: readonly Readonly<{
    id: string;
    sha256: string;
    sampleRate: number;
    channels: number;
    frames: number;
    inPointSeconds: number;
    scheduledDurationSeconds: number;
    decodedBytes: number;
  }>[];
  presentation?: Readonly<{
    width: number;
    height: number;
    devicePixelRatio: number;
    layers: number;
    expectedOpportunities: number;
    distinctPaintAcks: number;
    maximumConsecutiveMisses: number;
    coverageRatio: number;
    canvasBackingBytes: number;
    p95PaintAckMs: number;
    acknowledgedOutputFrames: readonly number[];
    missingOutputFrames: readonly number[];
    distinctPixelSignatures: number;
    duplicateCompositeOutputFrames: readonly number[];
    cadenceHoldOutputFrames: readonly number[];
    presentationAcks: readonly Readonly<{
      outputFrame: number;
      owner: "primary" | "secondary";
      primaryElementCurrentTime: number;
      secondaryElementCurrentTime: number;
      signature: number;
    }>[];
    stallControl: Readonly<{
      firstPaintAcknowledged: boolean;
      repeatedPaintAcknowledged: boolean;
    }>;
    elapsedTimelineSeconds: number;
    elapsedPresentationSeconds: number;
    playbackRateRatio: number | null;
  }>;
  resourceAccounting?: Readonly<{
    decodedAudioAssetCount: number;
    activeAndPrewarmAudioBufferBytes: number;
    canvasBackingBytes: number;
    scheduledSourceNodes: number;
    retainedDecodedBytesAfterStop: number;
  }>;
  observations: readonly Readonly<{
    ownerId: string;
    assetId: string;
    epoch: number;
    mediaTime: number;
    presentedFrames: number;
  }>[];
  decodedAudio: Readonly<{
    sampleRate: number;
    channels: number;
    length: number;
    duration: number;
    decodedBytes: number;
  }> | null;
  impulseControl: Readonly<{
    sampleIndex: number;
    lengthSamples: number;
    amplitude: number;
    channels: number;
    assetId?: string;
  }> | null;
  sampleRate: number;
  sourceCount: number;
  outputCorrelation: Readonly<{
    firstFrameCallbackTime: number;
    firstFramePresentationTime: number;
    firstFrameContextTime: number;
    scheduledContextTime: number;
    timelineMediaTimeAtSchedule: number;
    sourceInPointSeconds?: number;
    segmentDurationSeconds?: number;
    warmupOutputTimestamp: Readonly<{
      contextTime: number;
      performanceTime: number;
    }>;
  }> | null;
  baseLatency: number;
  outputLatency: number | null;
  cut: Readonly<{
    ownerId: string;
    assetId: string;
    videoMediaTime: number;
    decoderLocalMediaTime: number;
    rawVideoSourceMediaTime: number;
    admittedSourceInPointSeconds: number;
    videoCallbackTime: number;
    videoPresentationTime: number;
    audioBufferMediaTime: number | null;
    differenceSamples: number | null;
    signedDifferenceSamples?: number | null;
    outputTimestampContextTime: number;
    outputTimestampPerformanceTime: number;
    outputTimestampContextAtVideoFrame: number | null;
    secondAudioScheduledContextTime: number;
    cutSample: number;
  }> | null;
  callbacksAfterStop: number;
  presentationComplete?: boolean;
  started: boolean;
  stopped: boolean;
  contextState: string;
}>;

declare global {
  interface Window {
    __m25_56_native_pts_clock?: NativePtsClockProbe & {
      stop(): void;
      snapshot(): NativePtsClockProbe;
    };
    __m25_56_audio_context_buffer?: AudioContextBufferProbe & {
      prepared?: boolean;
      startError?: string | null;
      refusalBeforeCleanup?: AudioContextBufferProbe | null;
      contextTimeAt?: (
        performanceTime: number,
        timestamp?: Readonly<{ contextTime: number; performanceTime: number }>,
      ) => number | null;
      stop(): Promise<void>;
      snapshot(): AudioContextBufferProbe;
    };
  }
}

function summarizeTone(capture: AudioCapture) {
  const packetTimelineBreaks = capture.packets
    .map((packet, index) => ({
      packetIndex: index,
      devicePosition: packet.devicePosition,
      positionDeltaFrames: packet.positionDeltaFrames,
      qpcPosition: packet.qpcPosition,
      qpcDelta100ns: packet.qpcDelta100ns,
      qpcError100ns: packet.qpcError100ns,
      timelineGapFrames: packet.timelineGapFrames,
      frames: packet.frames,
      flags: packet.flags,
    }))
    .filter(
      (packet) =>
        (packet.positionDeltaFrames !== null &&
          packet.positionDeltaFrames !== 0) ||
        (packet.qpcDelta100ns !== null &&
          (packet.qpcDelta100ns <= 0 ||
            (packet.qpcError100ns !== null &&
              Math.abs(packet.qpcError100ns) > 10_000))) ||
        (packet.flags & 5) !== 0,
    );
  const toneWindows = capture.metrics.envelopes.filter(
    (envelope) =>
      envelope.channel_rms.every((rms) => rms >= 0.01 && rms <= 0.2) &&
      envelope.channel_frequency_hz.every(
        (frequency) => Math.abs(frequency - 440) <= 5,
      ),
  );
  const firstToneSample = toneWindows[0]?.start_sample ?? 0;
  const lastToneSample = toneWindows.at(-1)
    ? toneWindows.at(-1)!.start_sample + toneWindows.at(-1)!.sample_count
    : firstToneSample;
  const spanWindows = capture.metrics.envelopes.filter(
    (envelope) =>
      envelope.start_sample >= firstToneSample &&
      envelope.start_sample + envelope.sample_count <= lastToneSample,
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
  return {
    qualifyingToneWindows: toneWindows.length,
    packetTimelineValid: audioPacketTimelineViolation(capture) === null,
    packetTimeline: capture.packetTimeline,
    packetTimelineBreakCount: packetTimelineBreaks.length,
    packetTimelineBreakSamples: packetTimelineBreaks.slice(0, 16),
    packetClockSamples: capture.packets.slice(0, 20).map((packet, index) => ({
      packetIndex: index,
      time: packet.time,
      sampleStart: packet.sampleStart,
      qpcPosition: packet.qpcPosition,
      qpcDelta100ns: packet.qpcDelta100ns,
      qpcError100ns: packet.qpcError100ns,
      timelineGapFrames: packet.timelineGapFrames,
      frames: packet.frames,
      flags: packet.flags,
    })),
    qpcBreakCount: capture.packetTimeline.qpcBreaks,
    qpcBreakSamples: packetTimelineBreaks
      .filter(
        (packet) =>
          packet.qpcDelta100ns !== null &&
          (packet.qpcDelta100ns <= 0 ||
            (packet.qpcError100ns !== null &&
              Math.abs(packet.qpcError100ns) > 10_000)),
      )
      .slice(0, 16),
    samplesAnalyzed: capture.metrics.samples_analyzed,
    totalWindows: capture.metrics.envelopes.length,
    toneCoverageWithinToneSpan:
      spanWindows.length === 0 ? 0 : toneWindows.length / spanWindows.length,
    interiorSilenceRuns,
    maxInteriorSilenceSamples: Math.max(
      0,
      ...interiorSilenceRuns.map((run) => run.durationSamples),
    ),
    impulseEvents: capture.metrics.impulse_events.map((event) => ({
      channel: event.channel,
      startSample: event.start_sample,
      durationSamples: event.duration_samples,
      peak: event.peak,
    })),
    discontinuityPackets: capture.discontinuityPackets,
    timestampErrorPackets: capture.timestampErrorPackets,
    silentPackets: capture.silentPackets,
    channelMedianFrequencyHz: ([0, 1] as const).map((channel) => {
      const values = toneWindows
        .map((envelope) => envelope.channel_frequency_hz[channel])
        .sort((left, right) => left - right);
      return values[Math.floor(values.length / 2)] ?? 0;
    }),
    channelMeanRms: ([0, 1] as const).map(
      (channel) =>
        toneWindows.reduce(
          (total, envelope) => total + envelope.channel_rms[channel],
          0,
        ) / toneWindows.length,
    ),
  };
}

test.use({ launchOptions: { ignoreDefaultArgs: ["--mute-audio"] } });

test("M25-56 measures imported NLE preview audio against its known tone", async ({
  context,
  page,
}, testInfo) => {
  test.skip(
    process.platform !== "win32",
    "WASAPI process loopback is Windows-only",
  );
  let fixture: Awaited<ReturnType<typeof startImportFixture>> | undefined;
  let observer:
    Awaited<ReturnType<typeof startProcessAudioObserver>> | undefined;
  let directObserver:
    Awaited<ReturnType<typeof startProcessAudioObserver>> | undefined;
  try {
    await page.route("**/m25-56-direct-native-control.mp4", (route) =>
      route.fulfill({
        path: resolve(
          repositoryRoot,
          "tests/fixtures/m25_56_124f_tone_bt709.mp4",
        ),
        contentType: "video/mp4",
      }),
    );
    await page.goto("/");
    const browser = context.browser();
    if (!browser) throw new Error("owned Chromium browser required");
    const cdp = await browser.newBrowserCDPSession();
    let browserPid: number | undefined;
    try {
      const processInfo = await cdp.send("SystemInfo.getProcessInfo");
      browserPid = processInfo.processInfo.find(
        (row) => row.type === "browser",
      )?.id;
    } finally {
      await cdp.detach();
    }
    if (!Number.isInteger(browserPid) || !browserPid || browserPid < 1)
      throw new Error("owned browser process identity missing");
    directObserver = await startProcessAudioObserver(
      repositoryRoot,
      browserPid,
      processAudioObserverPath,
      processAudioObserverSha256,
    );
    await page.setContent(`
      <button id="start">Play direct native reference</button>
      <video id="source" playsinline preload="auto" src="/m25-56-direct-native-control.mp4"></video>
      <script>
        document.querySelector("#start").addEventListener("click", () => {
          const video = document.querySelector("#source");
          video.muted = false;
          video.volume = 1;
          void video.play();
        });
      </script>
    `);
    await page
      .getByRole("button", { name: "Play direct native reference" })
      .click();
    await page.waitForFunction(
      () =>
        ((document.querySelector("#source") as HTMLVideoElement | null)
          ?.currentTime ?? 0) >= 1.3,
      undefined,
      { timeout: 10_000 },
    );
    await page.evaluate(() =>
      (document.querySelector("#source") as HTMLVideoElement).pause(),
    );
    await page.waitForTimeout(350);
    const directCapture = await directObserver.stop();
    directObserver = undefined;
    const directTone = summarizeTone(directCapture);
    expect(directCapture.rawAudioRetained).toBe(false);
    expect(directCapture.microphoneOpened).toBe(false);
    expect(directTone.qualifyingToneWindows).toBeGreaterThanOrEqual(20);

    fixture = await startImportFixture(page);
    await page.goto(
      "/nleShell.html?import=1&target=absent&importMedia=m25_56_tone124",
    );
    await expectImportBootstrap(fixture);
    const pages = page.getByRole("navigation", { name: "H3 Context pages" });
    await pages.getByRole("button", { name: "Production" }).click();
    await page.getByRole("tab", { name: "Production" }).click();
    await pages.getByRole("button", { name: "Production" }).click();
    await page.getByRole("tab", { name: "Clip editor" }).click();
    await page.locator('[data-h3-nle-entry="open"]').click();
    await page.locator('[data-h3-nle-pane="assets"]').click();
    await page
      .locator('[data-h3-nle-control="asset.import_production"]')
      .click();
    await expect
      .poll(async () => (await shellSnapshot(page)).importStatus)
      .toBe("succeeded");
    // IMPORTANT: an asset-only V2 authoring workspace intentionally has no render snapshot until
    // a clip is inserted; read the authoring contract or valid imports look empty after upgrade.
    await expect
      .poll(
        async () =>
          (await shellSnapshot(page)).authoringStateV2?.assets.length ?? 0,
      )
      .toBeGreaterThan(0);
    const imported = await shellSnapshot(page);
    const assetId = imported.highlightedAssetIds[0];
    if (!assetId) throw new Error("imported asset identity missing");
    const importedAssets = imported.authoringStateV2?.assets;
    if (importedAssets === undefined)
      throw new Error("imported V2 authoring state missing");
    expect(importedAssets.some((asset) => asset.assetId === assetId)).toBe(
      true,
    );
    const importedCard = page.locator(`[data-h3-nle-asset="${assetId}"]`);
    await expect(importedCard).toBeVisible();
    await importedCard.locator('[data-h3-nle-control="asset.insert"]').click();
    await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);

    observer = await startProcessAudioObserver(
      repositoryRoot,
      browserPid,
      processAudioObserverPath,
      processAudioObserverSha256,
    );

    await page.locator('[data-h3-nle-control="transport.play"]').click();
    await expect
      .poll(async () => (await shellLastPresentation(page))?.frame ?? 0)
      .toBeGreaterThan(0);
    await page.waitForTimeout(1_600);
    await page.locator('[data-h3-nle-control="transport.pause"]').click();
    await page.waitForTimeout(350);
    const capture = await observer.stop();
    observer = undefined;
    const previewTone = summarizeTone(capture);
    expect(capture.rawAudioRetained).toBe(false);
    expect(capture.microphoneOpened).toBe(false);
    expect(capture.metrics.window_samples).toBe(480);
    expect(capture.metrics.samples_analyzed).toBeGreaterThan(48_000);
    expect(previewTone.qualifyingToneWindows).toBeGreaterThanOrEqual(20);
    for (const channel of [0, 1] as const)
      expect(
        capture.metrics.silence_runs.some(
          (run) =>
            run.channel === channel &&
            run.duration_samples >= 960 &&
            run.start_sample + run.duration_samples >=
              capture.metrics.samples_analyzed - 480,
        ),
      ).toBe(true);
    const evidence = {
      schema: "M25-56ImportedPreviewAudioV1",
      observerSha256: processAudioObserverSha256,
      browserExecutableSha256: capture.browserExecutableSha256,
      selector: capture.selector,
      assetId,
      sourceFingerprint: fixture.bootstrap()?.sourceFingerprint,
      sourceByteLength: fixture.bootstrap()?.sourceByteLength,
      directControl: directTone,
      preview: previewTone,
      frequencyDifferenceHz: previewTone.channelMedianFrequencyHz.map(
        (frequency, channel) =>
          Math.abs(frequency - directTone.channelMedianFrequencyHz[channel]!),
      ),
      rmsRelativeDifference: previewTone.channelMeanRms.map(
        (rms, channel) =>
          Math.abs(rms - directTone.channelMeanRms[channel]!) /
          directTone.channelMeanRms[channel]!,
      ),
      observerResolutionSamples: capture.metrics.window_samples,
      steadySilenceLimitSamples: 960,
      steadySilenceDisposition:
        previewTone.maxInteriorSilenceSamples >= 960 ? "RED" : "NO_RED_FOUND",
      resultKind: "BASELINE_MEASUREMENT_NOT_ACCEPTANCE",
      sampleRate: capture.sampleRate,
      windowSamples: capture.metrics.window_samples,
      samplesAnalyzed: capture.metrics.samples_analyzed,
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
    await testInfo.attach("m25-56-imported-preview-audio", {
      contentType: "application/json",
      body: Buffer.from(JSON.stringify(evidence)),
    });
    console.log(`M25-56_IMPORTED_PREVIEW_AUDIO=${JSON.stringify(evidence)}`);
  } finally {
    try {
      await observer?.stop();
    } finally {
      await directObserver?.stop();
      await fixture?.close();
    }
  }
});

test("M25-56 measures prewarmed native PTS owner handoff across a split cut", async ({
  context,
  page,
}, testInfo) => {
  test.skip(
    process.platform !== "win32",
    "WASAPI process loopback is Windows-only",
  );
  let observer:
    Awaited<ReturnType<typeof startProcessAudioObserver>> | undefined;
  try {
    await page.route("**/m25-56-pts-clock-prototype.mp4", (route) =>
      route.fulfill({
        path: resolve(
          repositoryRoot,
          "tests/fixtures/m25_56_124f_tone_bt709.mp4",
        ),
        contentType: "video/mp4",
      }),
    );
    await page.goto("/");
    const browser = context.browser();
    if (!browser) throw new Error("owned Chromium browser required");
    const cdp = await browser.newBrowserCDPSession();
    let browserPid: number | undefined;
    try {
      const processInfo = await cdp.send("SystemInfo.getProcessInfo");
      browserPid = processInfo.processInfo.find(
        (row) => row.type === "browser",
      )?.id;
    } finally {
      await cdp.detach();
    }
    if (!Number.isInteger(browserPid) || !browserPid || browserPid < 1)
      throw new Error("owned browser process identity missing");
    observer = await startProcessAudioObserver(
      repositoryRoot,
      browserPid,
      processAudioObserverPath,
      processAudioObserverSha256,
    );
    await page.setContent(`
      <button id="start">Start native PTS clock</button>
      <video id="primary" playsinline preload="auto" width="320" height="180" src="/m25-56-pts-clock-prototype.mp4"></video>
      <video id="secondary" playsinline preload="auto" width="320" height="180" src="/m25-56-pts-clock-prototype.mp4"></video>
      <script>
        const primary = document.querySelector("#primary");
        const secondary = document.querySelector("#secondary");
        const callbackIds = { primary: null, secondary: null };
        const media = { primary, secondary };
        const state = {
          observations: [],
          playCalls: 0,
          pauseCalls: 0,
          seekingEvents: 0,
          warmupPlayCalls: 0,
          warmupPauseCalls: 0,
          warmupSeekingEvents: 0,
          warmupComplete: false,
          started: false,
          activePlayback: false,
          callbacksAfterStop: 0,
          cut: null,
          activeOwner: "primary",
          stopped: false,
          stop() {
            if (this.stopped) return;
            this.stopped = true;
            for (const owner of ["primary", "secondary"]) {
              const video = media[owner];
              video.muted = true;
              video.pause();
              if (callbackIds[owner] !== null && typeof video.cancelVideoFrameCallback === "function") {
                video.cancelVideoFrameCallback(callbackIds[owner]);
                callbackIds[owner] = null;
              }
            }
          },
          snapshot() {
            return {
              observations: this.observations.map((item) => ({ ...item })),
              playCalls: this.playCalls,
              pauseCalls: this.pauseCalls,
              seekingEvents: this.seekingEvents,
              warmupPlayCalls: this.warmupPlayCalls,
              warmupPauseCalls: this.warmupPauseCalls,
              warmupSeekingEvents: this.warmupSeekingEvents,
              warmupComplete: this.warmupComplete,
              started: this.started,
              callbacksAfterStop: this.callbacksAfterStop,
              cut: this.cut === null ? null : { ...this.cut },
              stopped: this.stopped,
            };
          },
        };
        for (const video of [primary, secondary]) {
          video.addEventListener("seeking", () => {
            if (state.activePlayback) state.seekingEvents++;
            else state.warmupSeekingEvents++;
          });
          video.addEventListener("pause", () => state.pauseCalls++);
        }
        const observe = (owner, callbackTime, metadata) => {
          const video = media[owner];
          callbackIds[owner] = null;
          if (state.stopped) {
            state.callbacksAfterStop++;
            return;
          }
          if (
            owner === "primary" &&
            state.cut === null &&
            metadata.mediaTime >= 0.5
          ) {
            state.cut = {
              primaryMediaTime: metadata.mediaTime,
              secondaryMediaTime: secondary.currentTime,
              synchronizationDifferenceMs: Math.abs(secondary.currentTime - metadata.mediaTime) * 1_000,
              activeAudibleOwners: Number(!primary.muted) + Number(!secondary.muted),
            };
            primary.muted = true;
            primary.pause();
            secondary.muted = false;
            state.activeOwner = "secondary";
          }
          if (state.activeOwner === owner) {
            state.observations.push({
              ownerId: owner === "primary" ? "native-pts-primary-1" : "native-pts-secondary-2",
              assetId: owner === "primary" ? "vid-primary" : "vid-secondary",
              epoch: owner === "primary" ? 1 : 2,
              mediaTime: metadata.mediaTime,
              presentedFrames: metadata.presentedFrames,
              callbackTime,
            });
          }
          if (!video.paused && !state.stopped)
            callbackIds[owner] = video.requestVideoFrameCallback((now, next) => observe(owner, now, next));
        };
        window.__m25_56_native_pts_clock = state;
        document.querySelector("#start").addEventListener("click", () => {
          void (async () => {
            state.warmupPlayCalls++;
            secondary.muted = true;
            await secondary.play();
            await new Promise((resolve) =>
              secondary.requestVideoFrameCallback(() => resolve(undefined)),
            );
            secondary.pause();
            state.warmupPauseCalls++;
            await new Promise((resolve) => {
              secondary.addEventListener("seeked", () => resolve(undefined), {
                once: true,
              });
              secondary.currentTime = 0;
            });
            state.warmupComplete = true;
            state.activePlayback = true;
            state.playCalls += 2;
            primary.muted = false;
            secondary.muted = true;
            callbackIds.primary = primary.requestVideoFrameCallback((now, next) => observe("primary", now, next));
            callbackIds.secondary = secondary.requestVideoFrameCallback((now, next) => observe("secondary", now, next));
            await Promise.all([primary.play(), secondary.play()]);
            state.started = true;
          })();
        });
      </script>
    `);
    await page.waitForFunction(
      () =>
        ["#primary", "#secondary"].every(
          (selector) =>
            ((document.querySelector(selector) as HTMLVideoElement | null)
              ?.readyState ?? 0) >= 3,
        ),
      undefined,
      { timeout: 10_000 },
    );
    const prewarmReadyStates = await page.evaluate(() =>
      ["primary", "secondary"].map(
        (id) =>
          (document.querySelector(`#${id}`) as HTMLVideoElement | null)
            ?.readyState ?? 0,
      ),
    );
    await page.getByRole("button", { name: "Start native PTS clock" }).click();
    await page.waitForFunction(
      () => window.__m25_56_native_pts_clock?.started === true,
    );
    await page.waitForFunction(
      () =>
        ((document.querySelector("#secondary") as HTMLVideoElement | null)
          ?.currentTime ?? 0) >= 1.9,
      undefined,
      { timeout: 10_000 },
    );
    await page.evaluate(() => window.__m25_56_native_pts_clock!.stop());
    await page.waitForTimeout(350);
    const probe = await page.evaluate(() =>
      window.__m25_56_native_pts_clock!.snapshot(),
    );
    const capture = await observer.stop();
    observer = undefined;

    const initialAsset = splitSnapshotWithSecondOwner.assets.find(
      (candidate) => candidate.assetId === "vid-primary",
    );
    if (
      initialAsset?.sourceTimeBase === null ||
      initialAsset?.sourceTimeBase === undefined
    )
      throw new Error("split fixture source timebase is unavailable");
    const clock = new PtsSceneClockPrototype(splitSnapshotWithSecondOwner, {
      ownerId: "native-pts-primary-1",
      assetId: initialAsset.assetId,
      clipId: "clip-main",
      epoch: 1,
    });
    let currentEpoch = 1;
    const mapped = probe.observations.flatMap((sample) => {
      if (sample.epoch !== currentEpoch) {
        clock.replaceOwner({
          ownerId: sample.ownerId,
          assetId: sample.assetId,
          clipId: sample.epoch === 1 ? "clip-main" : "clip-main-right",
          epoch: sample.epoch,
        });
        currentEpoch = sample.epoch;
      }
      const asset = splitSnapshotWithSecondOwner.assets.find(
        (candidate) => candidate.assetId === sample.assetId,
      );
      if (asset?.sourceTimeBase === null || asset?.sourceTimeBase === undefined)
        return [];
      const observedSourcePts = Math.round(
        (sample.mediaTime * asset.sourceTimeBase!.den) /
          asset.sourceTimeBase!.num,
      );
      const result = clock.observe({
        ownerId: sample.ownerId,
        assetId: sample.assetId,
        clipId: sample.epoch === 1 ? "clip-main" : "clip-main-right",
        epoch: sample.epoch,
        observedSourcePts,
      });
      return result.disposition === "presented"
        ? [
            {
              mediaTime: sample.mediaTime,
              presentedFrames: sample.presentedFrames,
              frame: result.frame!,
              clipId: result.clipId!,
              layerCount: result.scene!.layers.length,
            },
          ]
        : [];
    });
    const beforeCut = mapped.filter((sample) => sample.frame < 12);
    const afterCut = mapped.filter((sample) => sample.frame >= 12);
    const audio = summarizeTone(capture);
    const cutDriftLimitMs = (2_000 / 48_000) * 1_000;
    const measuredViolations = [
      probe.cut === null ||
      probe.cut.synchronizationDifferenceMs > cutDriftLimitMs
        ? "native_owner_cut_av_drift_exceeds_2000_samples"
        : null,
      audio.maxInteriorSilenceSamples >= 960
        ? "steady_region_silence_exceeds_960_samples"
        : null,
      audioPacketTimelineViolation(capture),
    ].filter((value): value is string => value !== null);
    const evidence = {
      schema: "M25-56NativePtsSceneClockPrototypeV1",
      observerSha256: processAudioObserverSha256,
      browserExecutableSha256: capture.browserExecutableSha256,
      selector: capture.selector,
      fixture: "tests/fixtures/m25_56_124f_tone_bt709.mp4",
      snapshotFixture: "tests/fixtures/m25_16_scene_resolver_parity_v1.json",
      logicalSourceAssets: ["vid-primary", "vid-secondary"],
      prewarmReadyStateThreshold: 3,
      prewarmReadyStates,
      sourceTimeBase: initialAsset.sourceTimeBase,
      outputFrameRate: splitSnapshotWithSecondOwner.output.frameRate,
      cutOutputFrame: 12,
      cutClipIds: ["clip-main", "clip-main-right"],
      nativeProbe: {
        playCalls: probe.playCalls,
        pauseCalls: probe.pauseCalls,
        seekingEvents: probe.seekingEvents,
        warmupPlayCalls: probe.warmupPlayCalls,
        warmupPauseCalls: probe.warmupPauseCalls,
        warmupSeekingEvents: probe.warmupSeekingEvents,
        warmupComplete: probe.warmupComplete,
        started: probe.started,
        callbacksAfterStop: probe.callbacksAfterStop,
        cut: probe.cut,
        stopped: probe.stopped,
        observations: probe.observations.length,
        mappedSceneFrames: mapped.length,
        firstMapped: mapped[0] ?? null,
        lastMapped: mapped.at(-1) ?? null,
        beforeCutFrames: beforeCut.length,
        afterCutFrames: afterCut.length,
        sceneLayerMaximum: Math.max(
          0,
          ...mapped.map((sample) => sample.layerCount),
        ),
        mappedScenes: mapped,
      },
      audio: {
        sampleRate: capture.sampleRate,
        windowSamples: capture.metrics.window_samples,
        packetTimeline: capture.packetTimeline,
        packetTimelineValid: audio.packetTimelineValid,
        packetTimelineBreakCount: audio.packetTimelineBreakCount,
        packetTimelineBreakSamples: audio.packetTimelineBreakSamples,
        qualifyingToneWindows: audio.qualifyingToneWindows,
        channelMedianFrequencyHz: audio.channelMedianFrequencyHz,
        channelMeanRms: audio.channelMeanRms,
        maxInteriorSilenceSamples: audio.maxInteriorSilenceSamples,
        steadySilenceLimitSamples: 960,
        rawAudioRetained: capture.rawAudioRetained,
        microphoneOpened: capture.microphoneOpened,
      },
      measuredViolations,
      resultKind:
        measuredViolations.length === 0
          ? "PASS_NATIVE_HANDOFF_PROTOTYPE_NOT_PRODUCT_ACCEPTANCE"
          : "RED_NATIVE_HANDOFF_PROTOTYPE_NOT_PRODUCT_ACCEPTANCE",
      unproven: [
        "distinct source bytes, codecs, and nonzero source in-points at the boundary",
        "gap/image monotonic-clock fallback",
        "seek/replacement/close cancellation across product leases",
        "1920x1080 DPR1 product compositor presentation budget",
        "supported multilayer resource budget",
      ],
    };
    await testInfo.attach("m25-56-native-pts-owner-handoff", {
      contentType: "application/json",
      body: Buffer.from(JSON.stringify(evidence)),
    });
    console.log(`M25-56_NATIVE_HANDOFF=${JSON.stringify(evidence)}`);
    expect(capture.rawAudioRetained).toBe(false);
    expect(capture.microphoneOpened).toBe(false);
    expect(probe.playCalls).toBe(2);
    expect(probe.pauseCalls).toBeGreaterThanOrEqual(2);
    expect(probe.seekingEvents).toBe(0);
    expect(probe.cut).not.toBeNull();
    expect(probe.cut!.synchronizationDifferenceMs).toBeGreaterThanOrEqual(0);
    expect(probe.cut!.activeAudibleOwners).toBe(1);
    expect(probe.callbacksAfterStop).toBe(0);
    expect(probe.stopped).toBe(true);
    expect(mapped.length).toBeGreaterThanOrEqual(30);
    expect(beforeCut.some((sample) => sample.clipId === "clip-main")).toBe(
      true,
    );
    expect(afterCut.some((sample) => sample.clipId === "clip-main-right")).toBe(
      true,
    );
    expect(
      mapped.every(
        (sample, index) =>
          index === 0 || sample.frame > mapped[index - 1]!.frame,
      ),
    ).toBe(true);
    expect(audio.qualifyingToneWindows).toBeGreaterThanOrEqual(25);
    expect(prewarmReadyStates.every((readyState) => readyState >= 3)).toBe(
      true,
    );
    expect(
      measuredViolations.every((violation) =>
        [
          "native_owner_cut_av_drift_exceeds_2000_samples",
          "steady_region_silence_exceeds_960_samples",
          "audio_observer_packet_timeline_invalid",
        ].includes(violation),
      ),
    ).toBe(true);
    expect(
      measuredViolations.includes(
        "native_owner_cut_av_drift_exceeds_2000_samples",
      ),
    ).toBe(probe.cut!.synchronizationDifferenceMs > cutDriftLimitMs);
    expect(
      measuredViolations.includes("steady_region_silence_exceeds_960_samples"),
    ).toBe(audio.maxInteriorSilenceSamples >= 960);
  } finally {
    await observer?.stop();
  }
});

type CutQualificationFault =
  | "none"
  | "primary_callback_delay"
  | "native_play_delay"
  | "invalid_output_clock"
  | "stalled_output_clock"
  | "cut_timer_delay"
  | "load_rate_reset";

async function qualifyDecodedBufferCut(
  { context, page }: { context: BrowserContext; page: Page },
  testInfo: TestInfo,
  fault: CutQualificationFault = "none",
  expectedRefusal: string | null = null,
) {
  test.skip(
    process.platform !== "win32",
    "WASAPI process loopback is Windows-only",
  );
  let observer:
    Awaited<ReturnType<typeof startProcessAudioObserver>> | undefined;
  let diagnosticBeforeCleanup: AudioContextBufferProbe | null = null;
  let diagnosticStage = "setup";
  try {
    await page.addInitScript(installHeardCutCoordinatorPrototype);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.route("**/m25-56-audio-context-clock.mp4", (route) =>
      route.fulfill({
        path: primaryVideoFixturePath,
        contentType: "video/mp4",
      }),
    );
    await page.route("**/m25-56-audio-context-secondary.mp4", (route) =>
      route.fulfill({
        path: secondaryVideoFixturePath,
        contentType: "video/mp4",
      }),
    );
    await page.route("**/m25_56_audio_48khz_stereo_pcm.wav", (route) =>
      route.fulfill({
        path: audioContextPcmFixturePath,
        contentType: "audio/wav",
      }),
    );
    await page.route("**/m25_56_secondary_48khz_stereo_pcm.wav", (route) =>
      route.fulfill({
        path: secondaryAudioFixturePath,
        contentType: "audio/wav",
      }),
    );
    await page.goto("/");
    const browser = context.browser();
    if (!browser) throw new Error("owned Chromium browser required");
    const cdp = await browser.newBrowserCDPSession();
    let browserPid: number | undefined;
    try {
      const processInfo = await cdp.send("SystemInfo.getProcessInfo");
      browserPid = processInfo.processInfo.find(
        (row) => row.type === "browser",
      )?.id;
    } finally {
      await cdp.detach();
    }
    if (!Number.isInteger(browserPid) || !browserPid || browserPid < 1)
      throw new Error("owned browser process identity missing");
    observer = await startProcessAudioObserver(
      repositoryRoot,
      browserPid,
      processAudioObserverPath,
      processAudioObserverSha256,
    );
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.setContent(`
      <button id="start" style="position:fixed;top:8px;left:8px;z-index:10">Start bounded decoded-buffer clock</button>
      <canvas id="monitor" width="1920" height="1080" style="display:block;width:960px;height:540px"></canvas>
      <video id="clock" playsinline muted preload="auto" width="320" height="180" style="position:fixed;left:0;top:0;width:1px;height:1px;opacity:0" src="/m25-56-audio-context-clock.mp4"></video>
      <video id="secondary" playsinline muted preload="auto" width="320" height="180" style="position:fixed;left:1px;top:0;width:1px;height:1px;opacity:0" src="/m25-56-audio-context-secondary.mp4"></video>
      <script>
        const video = document.querySelector("#clock");
        const secondaryVideo = document.querySelector("#secondary");
        const canvas = document.querySelector("#monitor");
        const canvasContext = canvas.getContext("2d", { alpha: false });
        if (canvasContext === null) throw new Error("1080p monitor canvas unavailable");
        const ackCanvas = document.createElement("canvas");
        ackCanvas.width = 16;
        ackCanvas.height = 9;
        const ackContext = ackCanvas.getContext("2d", { alpha: false });
        if (ackContext === null) throw new Error("paint ACK sampler unavailable");
        const audioContext = new AudioContext({ sampleRate: 48000 });
        const qualificationFault = ${JSON.stringify(fault)};
        const state = {
          context: audioContext,
          coordinator: null,
          protocolRefusal: null,
          refusalBeforeCleanup: null,
          nativeHeldDecoder: null,
          heldCallbackId: null,
          startupTimerId: null,
          startupDelayTimerId: null,
          startupDelayResolve: null,
          startupCancel: null,
          startupEpoch: 0,
          stopPromise: null,
          refuseQualification(reason) {
            if (this.protocolRefusal !== null || this.stopped) return;
            this.protocolRefusal = { reason, performanceTime: performance.now() };
            this.traceEvent("protocol-refused", { reason });
            // IMPORTANT: freeze this owner's failing clock before async cleanup alters it.
            this.refusalBeforeCleanup = this.snapshot();
            void this.stop();
          },
          releaseSecondDecoder() {
            if (this.stopped || this.handoffStarted) return;
            this.handoffStarted = true;
            video.pause();
            if (this.callbackId !== null && this.callbackOwner === "primary")
              video.cancelVideoFrameCallback(this.callbackId);
            if (this.faultTimerId !== null) {
              clearTimeout(this.faultTimerId);
              this.faultTimerId = null;
            }
            this.callbackOwner = "secondary";
            this.callbackId = secondaryVideo.requestVideoFrameCallback((now, next) => this.deliverFrame("secondary", now, next));
            secondaryVideo.playbackRate = 1;
            const pair = this.readOutputClock("heard-release");
            this.traceEvent("heard-cut-release", { deadlineContextTime: this.secondAudioScheduledAt,
              releaseContextTime: this.contextTimeAt(performance.now(), pair),
              secondaryCurrentTime: secondaryVideo.currentTime, paused: secondaryVideo.paused });
          },
          async confirmHeldDecoder(epoch) {
            // IMPORTANT: resolve native startup and confirm admitted frame zero before PCM is
            // armed. readyState alone leaves play/compositor latency inside the audio cut.
            const firstFrame = new Promise((resolve) => {
              this.heldCallbackId = secondaryVideo.requestVideoFrameCallback((now, metadata) => {
                this.heldCallbackId = null;
                resolve({ now, metadata });
              });
            });
            // IMPORTANT: the already-presented initial frame emits no new rVFC at rate zero.
            // Reload the same pinned source before arm to obtain an actual frame-zero receipt.
            if (qualificationFault === "load_rate_reset") {
              secondaryVideo.playbackRate = 0;
              secondaryVideo.load();
            } else {
              secondaryVideo.load();
              // IMPORTANT: load resets the rate; setting zero before it starts an advancing decoder.
              secondaryVideo.playbackRate = 0;
            }
            const startup = (async () => {
              this.traceEvent("native-play-intent", { readyState: secondaryVideo.readyState, currentTime: secondaryVideo.currentTime });
              if (qualificationFault === "native_play_delay") {
                this.traceEvent("native-play-held", { delayMs: 80 });
                await new Promise((resolve) => {
                  this.startupDelayResolve = resolve;
                  this.startupDelayTimerId = setTimeout(() => {
                    this.startupDelayTimerId = null;
                    this.startupDelayResolve = null;
                    resolve();
                  }, 80);
                });
              }
              if (this.stopped || epoch !== this.startupEpoch) throw new Error("native_start_owner_disposed");
              this.traceEvent("native-play-invoked", { readyState: secondaryVideo.readyState, currentTime: secondaryVideo.currentTime });
              await secondaryVideo.play();
              this.traceEvent("native-play-resolved", { readyState: secondaryVideo.readyState, currentTime: secondaryVideo.currentTime });
              return await firstFrame;
            })();
            const first = await this.awaitNativeStart(startup);
            if (this.stopped || epoch !== this.startupEpoch) throw new Error("native_start_owner_disposed");
            this.traceEvent("native-held-observed", { mediaTime: first.metadata.mediaTime,
              currentTime: secondaryVideo.currentTime, paused: secondaryVideo.paused, playbackRate: secondaryVideo.playbackRate });
            if (Math.abs(first.metadata.mediaTime) > 1 / 48000 || secondaryVideo.paused
              || secondaryVideo.playbackRate !== 0 || Math.abs(secondaryVideo.currentTime) > 1 / 48000)
              throw new Error("native_held_origin_unconfirmed");
            this.nativeHeldDecoder = { mediaTime: first.metadata.mediaTime, currentTime: secondaryVideo.currentTime,
              confirmedPerformanceTime: performance.now(), paused: secondaryVideo.paused, playbackRate: secondaryVideo.playbackRate };
            this.traceEvent("native-held-confirmed", this.nativeHeldDecoder);
          },
          async awaitNativeStart(operation) {
            try {
              return await Promise.race([operation, new Promise((_, reject) => {
                this.startupTimerId = setTimeout(() => reject(new Error("native_held_frame_timeout")), 750);
              }), new Promise((_, reject) => {
                this.startupCancel = () => reject(new Error("native_start_owner_disposed"));
              })]);
            } finally {
              if (this.startupTimerId !== null) clearTimeout(this.startupTimerId);
              this.startupTimerId = null;
              this.startupCancel = null;
            }
          },
          clockDiagnostics: {
            fault: qualificationFault,
            totalClockReads: 0,
            samples: [],
            events: [],
            keyEvents: {},
            firstInvalidClock: null,
            lastAdvancePerformanceTime: null,
          },
          lastObservedClockContextTime: null,
          faultTimestamp: null,
          faultTimerId: null,
          traceEvent(name, fields = {}) {
            const row = { event: name, performanceTime: performance.now(), ...fields };
            this.clockDiagnostics.keyEvents[name] ??= row;
            this.clockDiagnostics.events.push(row);
            if (this.clockDiagnostics.events.length > 64) this.clockDiagnostics.events.shift();
          },
          readOutputClock(site, requestedPerformanceTime = performance.now()) {
            const native = this.context.getOutputTimestamp();
            const now = performance.now();
            let timestamp = native;
            if (this.started && now >= this.presentation.epochPerformanceTime + 200) {
              if (qualificationFault === "invalid_output_clock") {
                timestamp = { contextTime: 0, performanceTime: 0 };
              } else if (qualificationFault === "stalled_output_clock") {
                if (this.faultTimestamp === null) {
                  this.faultTimestamp = { ...native };
                  this.traceEvent("clock-frozen", { contextTime: native.contextTime, timestampPerformanceTime: native.performanceTime });
                }
                timestamp = this.faultTimestamp;
              }
            }
            const valid = Number.isFinite(timestamp.contextTime) && Number.isFinite(timestamp.performanceTime)
              && timestamp.contextTime > 0 && timestamp.performanceTime > 0
              && Math.abs(requestedPerformanceTime - timestamp.performanceTime) <= 100;
            if (valid && (this.lastObservedClockContextTime === null || timestamp.contextTime > this.lastObservedClockContextTime)) {
              this.lastObservedClockContextTime = timestamp.contextTime;
              this.clockDiagnostics.lastAdvancePerformanceTime = now;
            }
            const row = {
              site, readPerformanceTime: now, requestedPerformanceTime,
              contextTime: timestamp.contextTime, timestampPerformanceTime: timestamp.performanceTime,
              nativeContextTime: native.contextTime, nativeTimestampPerformanceTime: native.performanceTime,
              renderedContextTime: this.context.currentTime, contextState: this.context.state,
              afterArm: this.started, valid,
              sinceLastAdvanceMs: this.clockDiagnostics.lastAdvancePerformanceTime === null
                ? null : now - this.clockDiagnostics.lastAdvancePerformanceTime,
            };
            this.clockDiagnostics.totalClockReads++;
            this.clockDiagnostics.samples.push(row);
            if (this.clockDiagnostics.samples.length > 256) this.clockDiagnostics.samples.shift();
            if (!valid && this.started && this.clockDiagnostics.firstInvalidClock === null)
              this.clockDiagnostics.firstInvalidClock = { ...row };
            return timestamp;
          },
          deliverFrame(owner, callbackTime, metadata) {
            if (owner === "primary" && this.started && !this.handoffStarted
              && qualificationFault === "primary_callback_delay"
              && metadata.mediaTime >= this.audioStartMediaTime + 3 / 8) {
              this.traceEvent("primary-callback-held", { callbackTime, mediaTime: metadata.mediaTime, delayMs: 80 });
              this.faultTimerId = setTimeout(() => {
                this.faultTimerId = null;
                if (!this.stopped) void this.observe(owner, callbackTime, metadata);
              }, 80);
              return;
            }
            void this.observe(owner, callbackTime, metadata);
          },
          buffers: [],
          sourceNodes: [],
          scheduledSourceCount: 0,
          sourceAssets: [],
          presentation: {
            outputFrameRate: 24,
            outputEndExclusiveSeconds: 1.875,
            acknowledgedOutputFrames: new Set(),
            acknowledgedSignatures: new Set(),
            duplicateCompositeOutputFrames: new Set(),
            presentationAcks: new Map(),
            lastSignature: null,
            stallControl: null,
            ackDelaysMs: [],
            firstPresentationTime: null,
            lastPresentationTime: null,
            epochPerformanceTime: null,
          },
          resourceAccounting: null,
          observations: [],
          callbackId: null,
          callbackOwner: null,
          presentationLoopId: null,
          presentationComplete: false,
          decodedAudio: null,
          impulseControl: null,
          audioScheduledAt: null,
          audioStartMediaTime: null,
          secondAudioScheduledAt: null,
          // This value is injected from clip-main-right.sourceStartFrame through the admitted
          // asset landmark/time-base contract; decoder mapping, scene mapping and cut share it.
          secondarySourceAnchorMediaTime: ${admittedSecondarySourceInPoint.seconds},
          outputCorrelation: null,
          cut: null,
          handoffStarted: false,
          started: false,
          stopped: false,
          callbacksAfterStop: 0,
          startError: null,
          contextTimeAt(performanceTime, timestamp = this.readOutputClock("mapping", performanceTime)) {
            // IMPORTANT: Web Audio returns {0, 0} before its first rendered block; that is not a shared clock origin.
            if (
              !Number.isFinite(timestamp.contextTime) ||
              !Number.isFinite(timestamp.performanceTime) ||
              timestamp.contextTime <= 0 ||
              timestamp.performanceTime <= 0 ||
              Math.abs(performanceTime - timestamp.performanceTime) > 100
            ) return null;
            const mapped = timestamp.contextTime + (performanceTime - timestamp.performanceTime) / 1000;
            return Number.isFinite(mapped) ? mapped : null;
          },
          async waitForOutputClock() {
            const deadline = performance.now() + 1500;
            let previous = null;
            while (performance.now() < deadline) {
              const timestamp = this.readOutputClock("startup");
              if (
                timestamp.contextTime >= 0.05 &&
                timestamp.performanceTime > 0 &&
                previous !== null &&
                timestamp.contextTime > previous.contextTime &&
                timestamp.performanceTime > previous.performanceTime
              ) return { ...timestamp };
              previous = timestamp.contextTime >= 0.05 && timestamp.performanceTime > 0
                ? timestamp
                : null;
              await new Promise((resolve) => setTimeout(resolve, 20));
            }
            throw new Error("AudioContext output timestamp did not advance after resume");
          },
          videoPresentationTime(callbackTime, metadata) {
            if (Number.isFinite(metadata.expectedDisplayTime) && metadata.expectedDisplayTime > 0)
              return metadata.expectedDisplayTime;
            if (Number.isFinite(metadata.presentationTime) && metadata.presentationTime > 0)
              return metadata.presentationTime;
            return callbackTime;
          },
          async prepare() {
            const loadAudioAsset = async (id, url) => {
              const response = await fetch(url);
              if (!response.ok) throw new Error("decoded audio fixture request failed");
              const bytes = await response.arrayBuffer();
              const digest = await crypto.subtle.digest("SHA-256", bytes.slice(0));
              const sha256 = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
              const buffer = await this.context.decodeAudioData(bytes.slice(0));
              return { id, buffer, sha256 };
            };
            const [primaryAsset, secondaryAsset] = await Promise.all([
              loadAudioAsset("audio-primary-a", "/m25_56_audio_48khz_stereo_pcm.wav"),
              loadAudioAsset("audio-secondary-b", "/m25_56_secondary_48khz_stereo_pcm.wav"),
            ]);
            const sourceInPointSeconds = 0.25;
            const scheduledDurationSeconds = 0.5;
            const impulseSourceSample = Math.round(sourceInPointSeconds * primaryAsset.buffer.sampleRate);
            for (let channel = 0; channel < secondaryAsset.buffer.numberOfChannels; channel++) {
              secondaryAsset.buffer.getChannelData(channel).fill(0.8, impulseSourceSample, impulseSourceSample + 24);
            }
            this.buffers = [primaryAsset.buffer, secondaryAsset.buffer];
            this.sourceAssets = [primaryAsset, secondaryAsset].map((asset) => ({
              id: asset.id,
              sha256: asset.sha256,
              sampleRate: asset.buffer.sampleRate,
              channels: asset.buffer.numberOfChannels,
              frames: asset.buffer.length,
              inPointSeconds: sourceInPointSeconds,
              scheduledDurationSeconds,
              decodedBytes: asset.buffer.length * asset.buffer.numberOfChannels * Float32Array.BYTES_PER_ELEMENT,
            }));
            this.impulseControl = {
              sampleIndex: impulseSourceSample,
              lengthSamples: 24,
              amplitude: 0.8,
              channels: secondaryAsset.buffer.numberOfChannels,
              assetId: secondaryAsset.id,
            };
            this.decodedAudio = {
              sampleRate: primaryAsset.buffer.sampleRate,
              channels: primaryAsset.buffer.numberOfChannels,
              length: primaryAsset.buffer.length,
              duration: primaryAsset.buffer.duration,
              decodedBytes: this.sourceAssets.reduce((sum, asset) => sum + asset.decodedBytes, 0),
            };
            const waitForVideo = (node) => new Promise((resolve, reject) => {
              if (node.readyState >= 3) return resolve();
              node.addEventListener("canplay", resolve, { once: true });
              node.addEventListener("error", () => reject(new Error("video fixture failed to prewarm")), { once: true });
              node.load();
            });
            await Promise.all([waitForVideo(video), waitForVideo(secondaryVideo)]);
            this.prewarmReadyStates = [video.readyState, secondaryVideo.readyState];
            const controlTime = performance.now();
            const firstPaintAcknowledged = this.paintOutputFrame(
              "primary",
              -2,
              controlTime,
            );
            const repeatedPaintAcknowledged = this.paintOutputFrame(
              "primary",
              -1,
              controlTime,
            );
            this.presentation.stallControl = {
              firstPaintAcknowledged,
              repeatedPaintAcknowledged,
            };
            this.presentation.acknowledgedOutputFrames.clear();
            this.presentation.acknowledgedSignatures.clear();
            this.presentation.duplicateCompositeOutputFrames.clear();
            this.presentation.presentationAcks.clear();
            this.presentation.lastSignature = null;
            this.presentation.ackDelaysMs = [];
            this.presentation.firstPresentationTime = null;
            this.presentation.lastPresentationTime = null;
            this.resourceAccounting = {
              decodedAudioAssetCount: this.sourceAssets.length,
              activeAndPrewarmAudioBufferBytes: this.decodedAudio.decodedBytes,
              canvasBackingBytes: canvas.width * canvas.height * 4,
              scheduledSourceNodes: 2,
              retainedDecodedBytesAfterStop: 0,
            };
          },
          paintOutputFrame(owner, outputFrame, expectedDisplayTime) {
            const activeVideo = owner === "primary" ? video : secondaryVideo;
            const overlayVideo = owner === "primary" ? secondaryVideo : video;
            canvasContext.globalAlpha = 1;
            canvasContext.drawImage(activeVideo, 0, 0, canvas.width, canvas.height);
            canvasContext.globalAlpha = 0.4;
            canvasContext.drawImage(overlayVideo, 1280, 720, 640, 360);
            canvasContext.globalAlpha = 1;
            // Hash a downsample of the complete composite. A fixed center patch can remain static
            // even while another part of the presented frame changes.
            ackContext.drawImage(canvas, 0, 0, ackCanvas.width, ackCanvas.height);
            const pixels = ackContext.getImageData(0, 0, ackCanvas.width, ackCanvas.height).data;
            let signature = 2166136261;
            for (let index = 0; index < pixels.length; index += 4) {
              signature = Math.imul(signature ^ pixels[index], 16777619);
              signature = Math.imul(signature ^ pixels[index + 1], 16777619);
              signature = Math.imul(signature ^ pixels[index + 2], 16777619);
            }
            if (this.presentation.acknowledgedSignatures.has(signature)) {
              this.presentation.duplicateCompositeOutputFrames.add(outputFrame);
              return false;
            }
            // A fixed output-frame identity owns the denominator, while the sampled composite
            // pixels own the numerator. A paused or stalled source therefore cannot manufacture
            // coverage by repainting the same pixels under a later output-frame number.
            this.presentation.acknowledgedOutputFrames.add(outputFrame);
            this.presentation.acknowledgedSignatures.add(signature);
            this.presentation.presentationAcks.set(outputFrame, {
              outputFrame,
              owner,
              primaryElementCurrentTime: video.currentTime,
              secondaryElementCurrentTime: secondaryVideo.currentTime,
              signature,
            });
            this.presentation.lastSignature = signature;
            const paintedAt = performance.now();
            this.presentation.firstPresentationTime ??= paintedAt;
            this.presentation.lastPresentationTime = paintedAt;
            this.presentation.ackDelaysMs.push(
              Math.max(0, paintedAt - expectedDisplayTime),
            );
            return true;
          },
          startPresentationLoop() {
            const tick = (now) => {
              this.presentationLoopId = null;
              if (this.stopped) return;
              const contextTime = this.contextTimeAt(now);
              const timelineTime = contextTime === null
                ? null
                : contextTime - this.audioScheduledAt;
              if (
                timelineTime !== null &&
                timelineTime >= 0 &&
                timelineTime < this.presentation.outputEndExclusiveSeconds
              ) {
                const outputFrame = Math.floor(
                  timelineTime * this.presentation.outputFrameRate + 1e-7,
                );
                if (!this.presentation.acknowledgedOutputFrames.has(outputFrame)) {
                  const expectedDisplayTime =
                    this.presentation.epochPerformanceTime +
                    (outputFrame / this.presentation.outputFrameRate) * 1000;
                  this.paintOutputFrame(
                    outputFrame < 12 ? "primary" : "secondary",
                    outputFrame,
                    expectedDisplayTime,
                  );
                }
              }
              if (
                timelineTime === null ||
                timelineTime < this.presentation.outputEndExclusiveSeconds
              )
                this.presentationLoopId = requestAnimationFrame(tick);
              else {
                this.presentationComplete = true;
                this.coordinator?.stop();
              }
            };
            this.presentationLoopId = requestAnimationFrame(tick);
          },
          async observe(owner, callbackTime, metadata) {
            // IMPORTANT: a canceled primary callback must not clear the new secondary owner.
            if (owner === "primary" && this.handoffStarted) return;
            this.callbackId = null;
            this.callbackOwner = null;
            if (this.stopped) {
              this.callbacksAfterStop++;
              return;
            }
            const representedPerformanceTime = this.videoPresentationTime(callbackTime, metadata);
            this.traceEvent("callback-" + owner, {
              callbackTime, arrivalPerformanceTime: performance.now(), representedPerformanceTime,
              mediaTime: metadata.mediaTime, readyState: (owner === "primary" ? video : secondaryVideo).readyState,
            });
            const sourceMediaTime =
              owner === "primary"
                ? metadata.mediaTime
                : this.secondarySourceAnchorMediaTime +
                  metadata.mediaTime;
            const timelineTime = owner === "primary"
              ? metadata.mediaTime - this.audioStartMediaTime
              : 0.5 + metadata.mediaTime;
            if (owner === "secondary" && this.cut === null) {
              const videoPresentationTime = this.videoPresentationTime(callbackTime, metadata);
              const outputTimestamp = this.readOutputClock("secondary-cut", videoPresentationTime);
              const frameContextTime = this.contextTimeAt(videoPresentationTime, outputTimestamp);
              if (
                frameContextTime === null ||
                frameContextTime < this.secondAudioScheduledAt
              ) {
                this.callbackOwner = "secondary";
                this.callbackId = secondaryVideo.requestVideoFrameCallback((now, next) => this.deliverFrame("secondary", now, next));
                return;
              }
              const audioBufferMediaTime =
                0.5 + (frameContextTime - this.secondAudioScheduledAt);
              this.cut = {
                ownerId: "audio-context-secondary-2",
                assetId: "vid-secondary",
                videoMediaTime: timelineTime,
                decoderLocalMediaTime: metadata.mediaTime,
                rawVideoSourceMediaTime: sourceMediaTime,
                admittedSourceInPointSeconds:
                  this.secondarySourceAnchorMediaTime,
                audioBufferMediaTime,
                differenceSamples: Math.round(
                  Math.abs(audioBufferMediaTime - timelineTime) * 48000,
                ),
                signedDifferenceSamples: Math.round((audioBufferMediaTime - timelineTime) * 48000),
                videoCallbackTime: callbackTime,
                videoPresentationTime,
                outputTimestampContextTime: outputTimestamp.contextTime,
                outputTimestampPerformanceTime: outputTimestamp.performanceTime,
                outputTimestampContextAtVideoFrame: frameContextTime,
                secondAudioScheduledContextTime: this.secondAudioScheduledAt,
                cutSample: 24000,
              };
            }
            this.observations.push({
              ownerId: owner === "primary" ? "audio-context-primary-1" : "audio-context-secondary-2",
              assetId: owner === "primary" ? "vid-primary" : "vid-secondary",
              epoch: owner === "primary" ? 1 : 2,
              mediaTime: sourceMediaTime,
              presentedFrames: metadata.presentedFrames,
              timelineTime,
            });
            const activeVideo = owner === "primary" ? video : secondaryVideo;
            if (!activeVideo.paused && !this.stopped) {
              this.callbackOwner = owner;
              this.callbackId = activeVideo.requestVideoFrameCallback((now, next) => this.deliverFrame(owner, now, next));
            }
          },
          async start() {
            try {
              const epoch = ++this.startupEpoch;
              await this.context.resume();
              const warmupOutputTimestamp = await this.waitForOutputClock();
              video.muted = true;
              secondaryVideo.muted = true;
              await this.confirmHeldDecoder(epoch);
              if (this.stopped || epoch !== this.startupEpoch) return;
              const firstFrame = await this.awaitNativeStart(new Promise((resolve) => {
                this.callbackOwner = "primary";
                this.callbackId = video.requestVideoFrameCallback((now, metadata) => resolve({ now, metadata }));
                void video.play();
              }));
              const { now, metadata } = firstFrame;
              if (this.stopped || epoch !== this.startupEpoch) return;
              if (secondaryVideo.paused || secondaryVideo.playbackRate !== 0 || Math.abs(secondaryVideo.currentTime) > 1 / 48000)
                throw new Error("native_held_origin_lost_before_arm");
              const firstFramePresentationTime = this.videoPresentationTime(now, metadata);
              const frameContextTime = this.contextTimeAt(firstFramePresentationTime);
              if (frameContextTime === null)
                throw new Error("AudioContext output timestamp was unavailable at the first video frame");
              const startsAt = Math.max(this.context.currentTime + 0.02, frameContextTime + 0.02);
              const startMediaTime = metadata.mediaTime + (startsAt - frameContextTime);
              const sourceInPointSeconds = 0.25;
              const segmentDurationSeconds = 0.5;
              const first = this.context.createBufferSource();
              first.buffer = this.buffers[0];
              first.connect(this.context.destination);
              first.start(startsAt, sourceInPointSeconds, segmentDurationSeconds);
              const second = this.context.createBufferSource();
              second.buffer = this.buffers[1];
              second.connect(this.context.destination);
              second.start(
                startsAt + segmentDurationSeconds,
                sourceInPointSeconds,
                segmentDurationSeconds,
              );
              this.sourceNodes = [first, second];
              this.scheduledSourceCount = this.sourceNodes.length;
              this.audioScheduledAt = startsAt;
              this.audioStartMediaTime = startMediaTime;
              this.secondAudioScheduledAt = startsAt + segmentDurationSeconds;
              const outputTimestamp = this.readOutputClock("arm");
              this.presentation.epochPerformanceTime =
                outputTimestamp.performanceTime +
                (startsAt - outputTimestamp.contextTime) * 1000;
              this.outputCorrelation = {
                firstFrameCallbackTime: now,
                firstFramePresentationTime,
                firstFrameContextTime: frameContextTime,
                scheduledContextTime: startsAt,
                timelineMediaTimeAtSchedule: startMediaTime,
                sourceInPointSeconds,
                segmentDurationSeconds,
                warmupOutputTimestamp,
              };
              this.started = true;
              this.traceEvent("audio-armed", { scheduledContextTime: startsAt, cutContextTime: this.secondAudioScheduledAt, epochPerformanceTime: this.presentation.epochPerformanceTime });
              this.coordinator = window.__heardCutCoordinatorFactory({
                now: () => performance.now(),
                read: () => this.readOutputClock("deadline"),
                release: () => this.releaseSecondDecoder(),
                refuse: (reason) => this.refuseQualification(reason),
                setTimer: (callback, delayMs) => {
                  const heard = this.contextTimeAt(performance.now());
                  const delay = qualificationFault === "cut_timer_delay" && !this.handoffStarted
                    && heard !== null && heard >= this.secondAudioScheduledAt - 0.02 ? 80 : delayMs;
                  return setTimeout(callback, delay);
                },
                clearTimer: (id) => clearTimeout(id),
              });
              this.coordinator.arm(this.secondAudioScheduledAt);
              if (this.stopped) return;
              this.startPresentationLoop();
              void this.deliverFrame("primary", now, metadata);
            } catch (error) {
              this.refuseQualification(String(error.message ?? error));
            }
          },
          async stop() {
            // IMPORTANT: refusal starts async cleanup. Await its same promise on every
            // stop call; returning on stopped would claim release before close finishes.
            if (this.stopPromise !== null) return await this.stopPromise;
            this.stopPromise = (async () => {
            this.stopped = true;
            this.startupEpoch++;
            this.startupCancel?.();
            this.startupCancel = null;
            if (this.startupDelayTimerId !== null) clearTimeout(this.startupDelayTimerId);
            this.startupDelayTimerId = null;
            this.startupDelayResolve?.();
            this.startupDelayResolve = null;
            this.coordinator?.stop();
            if (this.heldCallbackId !== null) secondaryVideo.cancelVideoFrameCallback(this.heldCallbackId);
            this.heldCallbackId = null;
            if (this.startupTimerId !== null) clearTimeout(this.startupTimerId);
            this.startupTimerId = null;
            this.traceEvent("stop", { contextState: this.context.state, presentationComplete: this.presentationComplete });
            if (this.faultTimerId !== null) {
              clearTimeout(this.faultTimerId);
              this.faultTimerId = null;
            }
            video.muted = true;
            secondaryVideo.muted = true;
            video.pause();
            secondaryVideo.pause();
            const callbackVideo = this.callbackOwner === "secondary" ? secondaryVideo : video;
            if (this.callbackId !== null && typeof callbackVideo.cancelVideoFrameCallback === "function") {
              callbackVideo.cancelVideoFrameCallback(this.callbackId);
              this.callbackId = null;
              this.callbackOwner = null;
            }
            for (const source of this.sourceNodes) {
              try { source.stop(); } catch {}
            }
            if (this.context.state !== "closed") await this.context.close();
            if (this.presentationLoopId !== null) {
              cancelAnimationFrame(this.presentationLoopId);
              this.presentationLoopId = null;
            }
            this.sourceNodes = [];
            this.buffers = [];
            if (this.resourceAccounting !== null)
              this.resourceAccounting.retainedDecodedBytesAfterStop = this.buffers.reduce(
                (sum, buffer) =>
                  sum +
                  buffer.length *
                    buffer.numberOfChannels *
                    Float32Array.BYTES_PER_ELEMENT,
                0,
              );
            })();
            await this.stopPromise;
          },
          snapshot() {
            const sortedAckDelays = [...this.presentation.ackDelaysMs].sort((left, right) => left - right);
            const p95Index = sortedAckDelays.length === 0
              ? 0
              : Math.min(sortedAckDelays.length - 1, Math.ceil(sortedAckDelays.length * 0.95) - 1);
            const width = canvas.width;
            const height = canvas.height;
            const expectedOpportunities = Math.ceil(
              this.presentation.outputEndExclusiveSeconds *
                this.presentation.outputFrameRate,
            );
            const acknowledgedOutputFrames = [
              ...this.presentation.acknowledgedOutputFrames,
            ].sort((left, right) => left - right);
            const acknowledged = new Set(acknowledgedOutputFrames);
            const missingOutputFrames = Array.from(
              { length: expectedOpportunities },
              (_, frame) => frame,
            ).filter((frame) => !acknowledged.has(frame));
            const cadenceHoldOutputFrames = missingOutputFrames.filter((frame) =>
              this.presentation.duplicateCompositeOutputFrames.has(frame),
            );
            let maximumConsecutiveMisses = 0;
            let consecutiveMisses = 0;
            for (let frame = 0; frame < expectedOpportunities; frame++) {
              if (acknowledged.has(frame)) consecutiveMisses = 0;
              else {
                consecutiveMisses++;
                maximumConsecutiveMisses = Math.max(
                  maximumConsecutiveMisses,
                  consecutiveMisses,
                );
              }
            }
            const elapsedTimelineSeconds =
              (expectedOpportunities - 1) / this.presentation.outputFrameRate;
            const elapsedPresentationSeconds =
              this.presentation.firstPresentationTime === null ||
              this.presentation.lastPresentationTime === null
                ? 0
                : (this.presentation.lastPresentationTime -
                    this.presentation.firstPresentationTime) /
                  1000;
            return {
              ownedResources: {
                callbackCount: (this.callbackId === null ? 0 : 1) + (this.heldCallbackId === null ? 0 : 1),
                timerCount: (this.startupTimerId === null ? 0 : 1) + (this.startupDelayTimerId === null ? 0 : 1)
                  + (this.faultTimerId === null ? 0 : 1) + (this.coordinator?.snapshot().timerActive ? 1 : 0),
                sourceNodeCount: this.sourceNodes.length,
                retainedBufferBytes: this.buffers.reduce((sum, buffer) => sum + buffer.length * buffer.numberOfChannels * Float32Array.BYTES_PER_ELEMENT, 0),
              },
              protocolRefusal: this.protocolRefusal === null ? null : { ...this.protocolRefusal },
              heardCutProtocol: this.coordinator?.snapshot() ?? null,
              nativeHeldDecoder: this.nativeHeldDecoder === null ? null : { ...this.nativeHeldDecoder },
              clockDiagnostics: {
                ...this.clockDiagnostics,
                samples: this.clockDiagnostics.samples.map((row) => ({ ...row })),
                events: this.clockDiagnostics.events.map((row) => ({ ...row })),
                keyEvents: { ...this.clockDiagnostics.keyEvents },
              },
              observations: this.observations.map((sample) => ({ ...sample })),
              sourceAssets: this.sourceAssets.map((asset) => ({ ...asset })),
              presentation: {
                width,
                height,
                devicePixelRatio: window.devicePixelRatio,
                layers: 2,
                expectedOpportunities,
                distinctPaintAcks: acknowledgedOutputFrames.length,
                maximumConsecutiveMisses,
                coverageRatio: expectedOpportunities === 0
                  ? 0
                  : acknowledgedOutputFrames.length / expectedOpportunities,
                canvasBackingBytes: width * height * 4,
                p95PaintAckMs: sortedAckDelays[p95Index] ?? 0,
                acknowledgedOutputFrames,
                missingOutputFrames,
                distinctPixelSignatures:
                  this.presentation.acknowledgedSignatures.size,
                duplicateCompositeOutputFrames: [
                  ...this.presentation.duplicateCompositeOutputFrames,
                ].sort((left, right) => left - right),
                cadenceHoldOutputFrames,
                presentationAcks: [
                  ...this.presentation.presentationAcks.values(),
                ].sort((left, right) => left.outputFrame - right.outputFrame),
                stallControl: { ...this.presentation.stallControl },
                elapsedTimelineSeconds,
                elapsedPresentationSeconds,
                playbackRateRatio:
                  elapsedPresentationSeconds <= 0
                    ? null
                    : elapsedTimelineSeconds / elapsedPresentationSeconds,
              },
              resourceAccounting: { ...this.resourceAccounting },
              decodedAudio: this.decodedAudio === null ? null : { ...this.decodedAudio },
              impulseControl: this.impulseControl === null ? null : { ...this.impulseControl },
              sampleRate: this.context.sampleRate,
              sourceCount: this.scheduledSourceCount,
              outputCorrelation: this.outputCorrelation === null ? null : { ...this.outputCorrelation },
              baseLatency: this.context.baseLatency,
              outputLatency: typeof this.context.outputLatency === "number" ? this.context.outputLatency : null,
              cut: this.cut === null ? null : { ...this.cut },
              callbacksAfterStop: this.callbacksAfterStop,
              presentationComplete: this.presentationComplete,
              prewarmReadyStates: [...this.prewarmReadyStates],
              started: this.started,
              stopped: this.stopped,
              contextState: this.context.state,
            };
          },
        };
        window.__m25_56_audio_context_buffer = state;
        void state.prepare().then(() => { state.prepared = true; }).catch((error) => { state.startError = String(error); });
        document.querySelector("#start").addEventListener("click", () => { void state.start(); });
      </script>
    `);
    diagnosticStage = "prepare";
    await page
      .waitForFunction(
        () =>
          window.__m25_56_audio_context_buffer?.prepared === true ||
          Boolean(window.__m25_56_audio_context_buffer?.startError),
        undefined,
        { timeout: 4_000 },
      )
      .catch(() => undefined);
    const prepareState = await page.evaluate(() => ({
      prepared: window.__m25_56_audio_context_buffer?.prepared === true,
      startError: window.__m25_56_audio_context_buffer?.startError ?? null,
      videos: ["#clock", "#secondary"].map((selector) => {
        const video = document.querySelector(
          selector,
        ) as HTMLVideoElement | null;
        return {
          selector,
          present: video !== null,
          readyState: video?.readyState ?? null,
          networkState: video?.networkState ?? null,
          errorCode: video?.error?.code ?? null,
          currentSrc: video?.currentSrc ?? null,
        };
      }),
    }));
    expect({ ...prepareState, pageErrors }).toMatchObject({
      prepared: true,
      startError: null,
      pageErrors: [],
    });
    const suspendedClockMapping = await page.evaluate(
      () =>
        window.__m25_56_audio_context_buffer!.contextTimeAt?.(758.4, {
          contextTime: 0,
          performanceTime: 0,
        }) ?? null,
    );
    expect(suspendedClockMapping).toBeNull();
    await page
      .getByRole("button", { name: "Start bounded decoded-buffer clock" })
      .click();
    diagnosticStage = "start";
    await page.waitForFunction(
      () =>
        window.__m25_56_audio_context_buffer?.started === true ||
        Boolean(
          window.__m25_56_audio_context_buffer?.snapshot().protocolRefusal,
        ),
      undefined,
      { timeout: 10_000 },
    );
    if (expectedRefusal === null) {
      expect(
        await page.evaluate(
          () =>
            window.__m25_56_audio_context_buffer?.snapshot().protocolRefusal,
        ),
      ).toBeNull();
    }
    diagnosticStage = "presentation";
    await page.waitForFunction(
      () =>
        window.__m25_56_audio_context_buffer?.presentationComplete === true ||
        Boolean(
          window.__m25_56_audio_context_buffer?.snapshot().protocolRefusal,
        ),
      undefined,
      { timeout: 10_000 },
    );
    if (expectedRefusal !== null) {
      diagnosticBeforeCleanup = await page.evaluate(
        () =>
          window.__m25_56_audio_context_buffer!.refusalBeforeCleanup ??
          window.__m25_56_audio_context_buffer!.snapshot(),
      );
      await page.evaluate(() => window.__m25_56_audio_context_buffer!.stop());
      const refusalProbe = await page.evaluate(() =>
        window.__m25_56_audio_context_buffer!.snapshot(),
      );
      await observer.stop();
      observer = undefined;
      expect(refusalProbe.protocolRefusal?.reason).toBe(expectedRefusal);
      expect(refusalProbe.contextState).toBe("closed");
      expect(refusalProbe.ownedResources).toEqual({
        callbackCount: 0,
        timerCount: 0,
        sourceNodeCount: 0,
        retainedBufferBytes: 0,
      });
      expect(refusalProbe.callbacksAfterStop).toBe(0);
      expect(refusalProbe.presentationComplete).toBe(false);
      expect(pageErrors).toEqual([]);
      const keys = refusalProbe.clockDiagnostics!.keyEvents;
      if (fault === "invalid_output_clock") {
        const invalidRead =
          refusalProbe.clockDiagnostics!.firstInvalidClock!.readPerformanceTime;
        if (typeof invalidRead !== "number")
          throw new Error("invalid clock refusal is missing its read time");
        expect(
          refusalProbe.protocolRefusal!.performanceTime - invalidRead,
        ).toBeLessThanOrEqual(25);
      } else if (fault === "stalled_output_clock") {
        const frozenAt = keys["clock-frozen"]!.performanceTime;
        if (typeof frozenAt !== "number")
          throw new Error("frozen clock refusal is missing its event time");
        expect(
          refusalProbe.protocolRefusal!.performanceTime - frozenAt,
        ).toBeLessThanOrEqual(125);
      } else if (fault === "cut_timer_delay") {
        expect(refusalProbe.heardCutProtocol?.released).toBe(false);
        expect(keys["heard-cut-release"]).toBeUndefined();
      } else if (fault === "load_rate_reset") {
        expect(keys["native-held-observed"]!.playbackRate).toBe(1);
        expect(refusalProbe.sourceCount).toBe(0);
      }
      console.log(
        `DECODED_BUFFER_EXPECTED_REFUSAL=${JSON.stringify({ fault, expectedRefusal, reason: refusalProbe.protocolRefusal, ownedResources: refusalProbe.ownedResources })}`,
      );
      return;
    }
    expect(
      await page.evaluate(
        () => window.__m25_56_audio_context_buffer?.snapshot().protocolRefusal,
      ),
    ).toBeNull();
    diagnosticBeforeCleanup = await page.evaluate(() =>
      window.__m25_56_audio_context_buffer!.snapshot(),
    );
    diagnosticStage = "capture";
    await page.evaluate(() => window.__m25_56_audio_context_buffer!.stop());
    await page.waitForTimeout(350);
    const probe = await page.evaluate(() =>
      window.__m25_56_audio_context_buffer!.snapshot(),
    );
    const capture = await observer.stop();
    observer = undefined;
    expect(probe.nativeHeldDecoder).toMatchObject({
      mediaTime: 0,
      currentTime: 0,
      playbackRate: 0,
      paused: false,
    });
    expect(probe.heardCutProtocol?.released).toBe(true);
    expect(probe.ownedResources).toEqual({
      callbackCount: 0,
      timerCount: 0,
      sourceNodeCount: 0,
      retainedBufferBytes: 0,
    });
    if (fault === "primary_callback_delay")
      expect(
        probe.clockDiagnostics!.keyEvents["primary-callback-held"]!.delayMs,
      ).toBe(80);
    if (fault === "native_play_delay")
      expect(
        probe.clockDiagnostics!.keyEvents["native-play-held"]!.delayMs,
      ).toBe(80);

    diagnosticStage = "assertions";
    const initialAsset = splitSnapshotWithSecondOwner.assets.find(
      (candidate) => candidate.assetId === "vid-primary",
    );
    if (
      initialAsset?.sourceTimeBase === null ||
      initialAsset?.sourceTimeBase === undefined
    )
      throw new Error("split fixture source timebase is unavailable");
    const clock = new PtsSceneClockPrototype(splitSnapshotWithSecondOwner, {
      ownerId: "audio-context-primary-1",
      assetId: "vid-primary",
      clipId: "clip-main",
      epoch: 1,
    });
    let currentEpoch = 1;
    const sceneObservations = probe.observations.map((sample) => {
      if (sample.epoch !== currentEpoch) {
        clock.replaceOwner({
          ownerId: sample.ownerId,
          assetId: sample.assetId,
          clipId: sample.epoch === 1 ? "clip-main" : "clip-main-right",
          epoch: sample.epoch,
        });
        currentEpoch = sample.epoch;
      }
      const asset = splitSnapshotWithSecondOwner.assets.find(
        (candidate) => candidate.assetId === sample.assetId,
      );
      if (asset?.sourceTimeBase === null || asset?.sourceTimeBase === undefined)
        throw new Error(`source timebase unavailable for ${sample.assetId}`);
      const observedSourcePts = Math.round(
        (sample.mediaTime * asset.sourceTimeBase.den) /
          asset.sourceTimeBase.num,
      );
      const result = clock.observe({
        ownerId: sample.ownerId,
        assetId: sample.assetId,
        clipId: sample.epoch === 1 ? "clip-main" : "clip-main-right",
        epoch: sample.epoch,
        observedSourcePts,
      });
      return { sample, observedSourcePts, result };
    });
    const mapped = sceneObservations.flatMap(({ sample, result }) =>
      result.disposition === "presented"
        ? [
            {
              frame: result.frame!,
              clipId: result.clipId!,
              mediaTime: sample.mediaTime,
            },
          ]
        : [],
    );
    const cutSceneObservation =
      probe.cut === null
        ? null
        : (sceneObservations.find(
            ({ sample }) =>
              sample.ownerId === probe.cut!.ownerId &&
              sample.assetId === probe.cut!.assetId &&
              Math.abs(sample.mediaTime - probe.cut!.rawVideoSourceMediaTime) <=
                1 / 48,
          ) ?? null);
    const cutSceneAccepted =
      cutSceneObservation?.result.disposition === "presented" &&
      cutSceneObservation.result.clipId === "clip-main-right" &&
      cutSceneObservation.result.frame ===
        Math.round(probe.cut!.videoMediaTime * 24);
    const audio = summarizeTone(capture);
    const impulseEventsByChannel = ([0, 1] as const).map((channel) =>
      audio.impulseEvents.filter((event) => event.channel === channel),
    );
    const impulseControlPass = impulseEventsByChannel.every(
      (events) =>
        events.length === 1 &&
        events[0]!.durationSamples >= 24 &&
        events[0]!.durationSamples <= 48 &&
        events[0]!.peak >= 0.5 &&
        events[0]!.peak < 1,
    );
    const sourceAssetsDistinct =
      probe.sourceAssets?.length === 2 &&
      new Set(probe.sourceAssets.map((asset) => asset.sha256)).size === 2;
    const sourceInPointsPass =
      probe.sourceAssets?.length === 2 &&
      probe.sourceAssets.every(
        (asset) =>
          asset.inPointSeconds > 0 &&
          asset.scheduledDurationSeconds === 0.5 &&
          asset.frames > asset.inPointSeconds * asset.sampleRate,
      );
    const presentation = probe.presentation;
    const presentationShapePass =
      presentation?.width === 1920 &&
      presentation.height === 1080 &&
      presentation.devicePixelRatio === 1 &&
      presentation.layers === 2;
    const presentationCoveragePass =
      presentation !== undefined &&
      presentation.expectedOpportunities > 0 &&
      presentation.coverageRatio >= 0.95 &&
      presentation.maximumConsecutiveMisses <= 2 &&
      presentation.playbackRateRatio !== null &&
      presentation.playbackRateRatio >= 0.95 &&
      presentation.playbackRateRatio <= 1.05 &&
      presentation.distinctPaintAcks >=
        Math.ceil(presentation.expectedOpportunities * 0.95) &&
      presentation.distinctPixelSignatures === presentation.distinctPaintAcks &&
      presentation.presentationAcks.length === presentation.distinctPaintAcks &&
      presentation.stallControl.firstPaintAcknowledged &&
      !presentation.stallControl.repeatedPaintAcknowledged;
    const bufferReleasePass =
      probe.resourceAccounting?.decodedAudioAssetCount === 2 &&
      probe.resourceAccounting.retainedDecodedBytesAfterStop === 0 &&
      probe.contextState === "closed" &&
      probe.callbacksAfterStop === 0;
    const cutDriftLimitSamples = 2_000;
    const measuredViolations = [
      probe.cut === null ||
      probe.cut.differenceSamples === null ||
      probe.cut.differenceSamples > cutDriftLimitSamples
        ? "audio_context_video_drift_exceeds_2000_samples"
        : null,
      audio.maxInteriorSilenceSamples >= 960
        ? "audio_context_steady_silence_exceeds_960_samples"
        : null,
      !impulseControlPass
        ? "audio_context_cut_impulse_not_exactly_once_per_channel"
        : null,
      !sourceAssetsDistinct || !sourceInPointsPass
        ? "independent_nonzero_source_inpoints_unproven"
        : null,
      !presentationShapePass
        ? "1080p_dpr1_two_layer_presentation_probe_invalid"
        : null,
      !presentationCoveragePass
        ? "presentation_distinct_frame_coverage_below_95_percent_or_gap_over_2"
        : null,
      !cutSceneAccepted
        ? "audio_context_cut_not_accepted_by_admitted_scene_mapper"
        : null,
      !bufferReleasePass
        ? "audio_context_buffer_release_or_callback_teardown_failed"
        : null,
      probe.contextState !== "closed"
        ? "audio_context_not_closed_on_stop"
        : null,
      audioPacketTimelineViolation(capture),
    ].filter((value): value is string => value !== null);
    const evidence = {
      schema: "M25-56AudioContextMultiSourcePresentationPrototypeV2",
      sourceWav: "tests/fixtures/m25_56_audio_48khz_stereo_pcm.wav",
      sourceWavSha256: audioContextPcmFixtureSha256,
      sourceWavSecondary:
        "tests/fixtures/m25_56_secondary_48khz_stereo_pcm.wav",
      sourceWavSecondarySha256: secondaryAudioFixtureSha256,
      sourceMp4Primary: "tests/fixtures/m25_56_primary_motion_124f.mp4",
      sourceMp4PrimarySha256: primaryVideoFixtureSha256,
      sourceMp4Secondary:
        "tests/fixtures/m25_56_secondary_motion_from_frame12_112f.mp4",
      sourceMp4SecondarySha256: secondaryVideoFixtureSha256,
      sourceMp4SecondaryOrigin: {
        source: "tests/fixtures/m25_56_secondary_motion_124f.mp4",
        sourceSha256: secondaryVideoSourceFixtureSha256,
        sourceStartFrame: 12,
        sourceStartPts: admittedSecondarySourceInPoint.pts,
        sourceStartSeconds: admittedSecondarySourceInPoint.seconds,
        derivativeFrameZeroPts: 0,
        sourceFrame12DecodedMd5: "2a4c25fe928bdfc6d01ab55695ee9f9a", // pragma: allowlist secret
        derivativeFrame0DecodedMd5: "2a4c25fe928bdfc6d01ab55695ee9f9a", // pragma: allowlist secret
      },
      sourceTimeBase: initialAsset.sourceTimeBase,
      outputFrameRate: splitSnapshotWithSecondOwner.output.frameRate,
      cutOutputFrame: 12,
      cutSample: 24_000,
      browserExecutableSha256: capture.browserExecutableSha256,
      observerSha256: processAudioObserverSha256,
      selector: capture.selector,
      audioScheduling: {
        kind: "two independent pinned PCM buffers, both scheduled from nonzero 250 ms in-points",
        clockMapping:
          "advancing getOutputTimestamp (contextTime >= 50 ms) to rVFC expectedDisplayTime; zero/stale (>100 ms) timestamps rejected",
        sourceAssets: probe.sourceAssets,
        decodedAudio: probe.decodedAudio,
        impulseControl: probe.impulseControl,
        outputCorrelation: probe.outputCorrelation,
        baseLatency: probe.baseLatency,
        outputLatency: probe.outputLatency,
        contextSampleRate: probe.sampleRate,
        sourceNodeCount: probe.sourceCount,
        resourceAccounting: probe.resourceAccounting,
      },
      nativeVideo: {
        observations: probe.observations.length,
        mappedSceneFrames: mapped.length,
        firstMapped: mapped[0] ?? null,
        lastMapped: mapped.at(-1) ?? null,
        admittedSecondarySourceInPoint: {
          pts: admittedSecondarySourceInPoint.pts,
          seconds: admittedSecondarySourceInPoint.seconds,
        },
        cutScene:
          cutSceneObservation === null
            ? null
            : {
                ownerId: cutSceneObservation.sample.ownerId,
                assetId: cutSceneObservation.sample.assetId,
                rawVideoSourceMediaTime: cutSceneObservation.sample.mediaTime,
                observedSourcePts: cutSceneObservation.observedSourcePts,
                admittedSourceStartPts: admittedSecondarySourceInPoint.pts,
                disposition: cutSceneObservation.result.disposition,
                mappedOutputFrame: cutSceneObservation.result.frame,
                mappedClipId: cutSceneObservation.result.clipId,
              },
        cut: probe.cut,
        callbacksAfterStop: probe.callbacksAfterStop,
        contextStateAfterStop: probe.contextState,
      },
      presentation: probe.presentation,
      audioOutput: {
        sampleRate: capture.sampleRate,
        windowSamples: capture.metrics.window_samples,
        packetTimeline: capture.packetTimeline,
        packetTimelineValid: audio.packetTimelineValid,
        packetTimelineBreakCount: audio.packetTimelineBreakCount,
        packetTimelineBreakSamples: audio.packetTimelineBreakSamples,
        qualifyingToneWindows: audio.qualifyingToneWindows,
        channelMedianFrequencyHz: audio.channelMedianFrequencyHz,
        channelMeanRms: audio.channelMeanRms,
        maxInteriorSilenceSamples: audio.maxInteriorSilenceSamples,
        steadySilenceLimitSamples: 960,
        impulseEvents: audio.impulseEvents,
        expectedImpulseEventsPerChannel: 1,
        impulseControlPass,
        rawAudioRetained: capture.rawAudioRetained,
        microphoneOpened: capture.microphoneOpened,
      },
      measuredViolations,
      resultKind:
        measuredViolations.length === 0
          ? "PASS_AUDIO_CONTEXT_BUFFER_PROTOTYPE_NOT_PRODUCT_ACCEPTANCE"
          : "PARTIAL_AUDIO_CONTEXT_BUFFER_PROTOTYPE_NOT_PRODUCT_ACCEPTANCE",
      unproven: [
        "demux/decode of arbitrary admitted source bytes through the production lease",
        "shipping-editor integration of the separately qualified owner lifecycle prototype",
        "the actual product compositor's 1920x1080 DPR1 budget under supported multilayer/edit load",
        "decoder/GPU memory maxima for representative source durations beyond the two pinned fixtures",
      ],
    };
    await testInfo.attach("m25-56-audio-context-buffer-prototype", {
      contentType: "application/json",
      body: Buffer.from(JSON.stringify(evidence)),
    });
    console.log(`M25-56_AUDIO_CONTEXT_BUFFER=${JSON.stringify(evidence)}`);
    expect(capture.rawAudioRetained).toBe(false);
    expect(capture.microphoneOpened).toBe(false);
    expect(probe.decodedAudio).not.toBeNull();
    expect(probe.decodedAudio!.sampleRate).toBe(48_000);
    expect(probe.decodedAudio!.channels).toBe(2);
    expect(probe.sourceAssets).toHaveLength(2);
    expect(new Set(probe.sourceAssets!.map((asset) => asset.sha256)).size).toBe(
      2,
    );
    expect(
      probe.sourceAssets!.every(
        (asset) =>
          asset.inPointSeconds > 0 &&
          asset.scheduledDurationSeconds > 0 &&
          asset.decodedBytes > 0,
      ),
    ).toBe(true);
    expect(probe.sourceAssets!.map((asset) => asset.inPointSeconds)).toEqual([
      0.25, 0.25,
    ]);
    expect(probe.presentation).toMatchObject({
      width: 1920,
      height: 1080,
      devicePixelRatio: 1,
      layers: 2,
    });
    expect(probe.presentation!.coverageRatio).toBeGreaterThanOrEqual(0.95);
    expect(probe.presentation!.maximumConsecutiveMisses).toBeLessThanOrEqual(2);
    expect(probe.presentation!.expectedOpportunities).toBe(45);
    expect(probe.presentation!.playbackRateRatio).toBeGreaterThanOrEqual(0.95);
    expect(probe.presentation!.playbackRateRatio).toBeLessThanOrEqual(1.05);
    expect(probe.presentation!.distinctPaintAcks).toBeGreaterThanOrEqual(
      Math.ceil(probe.presentation!.expectedOpportunities * 0.95),
    );
    expect(probe.presentation!.distinctPixelSignatures).toBe(
      probe.presentation!.distinctPaintAcks,
    );
    expect(probe.presentation!.presentationAcks).toHaveLength(
      probe.presentation!.distinctPaintAcks,
    );
    expect(
      new Set(
        probe.presentation!.presentationAcks.map(
          (acknowledgement) => acknowledgement.signature,
        ),
      ).size,
    ).toBe(probe.presentation!.distinctPaintAcks);
    expect(
      probe.presentation!.cadenceHoldOutputFrames.every((frame) =>
        probe.presentation!.missingOutputFrames.includes(frame),
      ),
    ).toBe(true);
    expect(probe.presentation!.stallControl).toEqual({
      firstPaintAcknowledged: true,
      repeatedPaintAcknowledged: false,
    });
    expect(
      probe.prewarmReadyStates!.every((readyState) => readyState >= 3),
    ).toBe(true);
    expect(probe.resourceAccounting).toMatchObject({
      decodedAudioAssetCount: 2,
      canvasBackingBytes: 1920 * 1080 * 4,
      scheduledSourceNodes: 2,
      retainedDecodedBytesAfterStop: 0,
    });
    expect(probe.sourceCount).toBe(2);
    expect(probe.cut).toMatchObject({
      ownerId: "audio-context-secondary-2",
      assetId: "vid-secondary",
      admittedSourceInPointSeconds: admittedSecondarySourceInPoint.seconds,
      cutSample: 24_000,
    });
    expect(admittedSecondarySourceInPoint).toEqual({
      pts: 6_144,
      seconds: 0.5,
    });
    expect(probe.cut!.rawVideoSourceMediaTime).toBeGreaterThanOrEqual(
      admittedSecondarySourceInPoint.seconds,
    );
    expect(
      Math.abs(
        probe.cut!.rawVideoSourceMediaTime -
          (admittedSecondarySourceInPoint.seconds +
            probe.cut!.decoderLocalMediaTime),
      ),
    ).toBeLessThanOrEqual(1 / 12_288);
    expect(cutSceneAccepted).toBe(true);
    expect(cutSceneObservation?.result).toMatchObject({
      disposition: "presented",
      clipId: "clip-main-right",
      frame: Math.round(probe.cut!.videoMediaTime * 24),
    });
    expect(probe.cut!.videoMediaTime).toBeGreaterThanOrEqual(0.5);
    expect(probe.cut!.secondAudioScheduledContextTime).toBeGreaterThan(
      probe.outputCorrelation!.scheduledContextTime,
    );
    // Compare both clocks on the admitted composition timeline. Callback arrival alone can be
    // later than the represented video frame and is retained above only as diagnostic evidence.
    expect(probe.cut!.differenceSamples).toBeLessThanOrEqual(2_000);
    expect(probe.callbacksAfterStop).toBe(0);
    expect(probe.contextState).toBe("closed");
    expect(measuredViolations).toEqual([]);
    expect(mapped.length).toBeGreaterThanOrEqual(30);
    expect(
      mapped.every(
        (sample, index) =>
          index === 0 || sample.frame > mapped[index - 1]!.frame,
      ),
    ).toBe(true);
    expect(audio.qualifyingToneWindows).toBeGreaterThanOrEqual(25);
  } finally {
    // IMPORTANT: retain the failing owner's clock before stop/page teardown; a later run
    // cannot supply the missing discriminator for this run's cut or progress timeout.
    let afterCleanup: AudioContextBufferProbe | null = null;
    let snapshotDisposition = "captured";
    try {
      if (page.isClosed()) throw new Error("page disposed");
      diagnosticBeforeCleanup ??= await page.evaluate(
        () =>
          window.__m25_56_audio_context_buffer?.refusalBeforeCleanup ??
          window.__m25_56_audio_context_buffer?.snapshot() ??
          null,
      );
      await page.evaluate(() => window.__m25_56_audio_context_buffer?.stop());
      afterCleanup = await page.evaluate(
        () => window.__m25_56_audio_context_buffer?.snapshot() ?? null,
      );
      if (diagnosticBeforeCleanup === null)
        snapshotDisposition = "probe_unavailable";
    } catch {
      snapshotDisposition = "page_or_probe_unavailable";
    }
    try {
      await observer?.stop();
    } finally {
      const diagnostics = {
        schema: "decoded_buffer_cut_clock_diagnostics.v1",
        stage: diagnosticStage,
        snapshotDisposition,
        beforeCleanup: diagnosticBeforeCleanup,
        afterCleanup,
      };
      await testInfo.attach("decoded-buffer-cut-clock-diagnostics", {
        contentType: "application/json",
        body: Buffer.from(JSON.stringify(diagnostics)),
      });
      console.log(
        `DECODED_BUFFER_CUT_CLOCK_DIAGNOSTICS=${JSON.stringify(diagnostics)}`,
      );
    }
  }
}

// IMPORTANT: Playwright reflects fixture destructuring; a plain owner argument fails collection.
test("M25-56 evaluates bounded AudioContext decoded-buffer split scheduling", async ({
  context,
  page,
}, testInfo) => {
  await qualifyDecodedBufferCut({ context, page }, testInfo);
});

for (const entry of [
  {
    title: "heard cut tolerates an actually delayed primary callback",
    fault: "primary_callback_delay",
    refusal: null,
  },
  {
    title: "heard cut confirms delayed native startup before scheduling PCM",
    fault: "native_play_delay",
    refusal: null,
  },
  {
    title: "heard cut refuses an invalid output clock with completed cleanup",
    fault: "invalid_output_clock",
    refusal: "output_clock_invalid",
  },
  {
    title: "heard cut refuses a frozen output clock within its progress bound",
    fault: "stalled_output_clock",
    refusal: "output_clock_stalled",
  },
  {
    title:
      "heard cut refuses an expired release deadline without switching owner",
    fault: "cut_timer_delay",
    refusal: "cut_deadline_missed",
  },
  {
    title: "held native startup rejects load resetting its held playback rate",
    fault: "load_rate_reset",
    refusal: "native_held_origin_unconfirmed",
  },
] as const) {
  test(entry.title, async ({ context, page }, testInfo) => {
    await qualifyDecodedBufferCut(
      { context, page },
      testInfo,
      entry.fault,
      entry.refusal,
    );
  });
}

test("M25-56 decoded-buffer owner cancels pause, seek, replacement, context loss, and close", async ({
  page,
}, testInfo) => {
  await page.setContent(
    '<button id="activate">Activate lifecycle probe</button>',
  );
  await page.locator("#activate").click();
  const evidence = await page.evaluate(async () => {
    const context = new AudioContext({ sampleRate: 48_000 });
    await context.resume();
    let ownedBuffer: AudioBuffer | null = context.createBuffer(
      2,
      48_000,
      48_000,
    );
    for (let channel = 0; channel < ownedBuffer.numberOfChannels; channel++)
      ownedBuffer.getChannelData(channel).fill(0.01);
    const sourceMetadata = {
      sampleRate: ownedBuffer.sampleRate,
      channels: ownedBuffer.numberOfChannels,
      frames: ownedBuffer.length,
    };
    const retainedDecodedBytes =
      ownedBuffer.length *
      ownedBuffer.numberOfChannels *
      Float32Array.BYTES_PER_ELEMENT;
    const events: Array<Readonly<Record<string, unknown>>> = [];
    let epoch = 0;
    let activeNodes: AudioBufferSourceNode[] = [];
    let callbackTimer: ReturnType<typeof setTimeout> | null = null;
    let callbackCommits = 0;
    let callbacksAfterCancellation = 0;
    let maximumActiveNodes = 0;
    let retainedBytesAfterClose = retainedDecodedBytes;
    const ownedBufferBytes = () =>
      ownedBuffer === null
        ? 0
        : ownedBuffer.length *
          ownedBuffer.numberOfChannels *
          Float32Array.BYTES_PER_ELEMENT;

    const cancelOwner = (reason: string) => {
      epoch += 1;
      if (callbackTimer !== null) {
        clearTimeout(callbackTimer);
        callbackTimer = null;
      }
      for (const source of activeNodes) {
        try {
          source.stop();
        } catch {}
        source.disconnect();
      }
      activeNodes = [];
      events.push({
        reason,
        epoch,
        activeNodes: 0,
        contextState: context.state,
      });
    };
    const startOwner = (ownerId: string, inPointSeconds: number) => {
      cancelOwner(`replace:${ownerId}`);
      if (ownedBuffer === null)
        throw new Error("decoded buffer owner was already released");
      const ownerEpoch = epoch;
      const source = context.createBufferSource();
      source.buffer = ownedBuffer;
      source.connect(context.destination);
      source.start(context.currentTime + 0.01, inPointSeconds, 0.25);
      activeNodes = [source];
      maximumActiveNodes = Math.max(maximumActiveNodes, activeNodes.length);
      callbackTimer = setTimeout(() => {
        if (ownerEpoch !== epoch) callbacksAfterCancellation += 1;
        else callbackCommits += 1;
      }, 35);
      events.push({
        reason: "start",
        ownerId,
        epoch,
        inPointSeconds,
        activeNodes: activeNodes.length,
      });
    };

    startOwner("owner-initial", 0.25);
    await new Promise((resolve) => setTimeout(resolve, 50));
    cancelOwner("pause");
    // Gap/image intervals have no media owner. Advance a real bounded scene coordinator from one
    // monotonic anchor and record the independently expected 24 fps output-frame identities.
    const staticCanvas = document.createElement("canvas");
    staticCanvas.width = 320;
    staticCanvas.height = 180;
    const staticContext = staticCanvas.getContext("2d", { alpha: false });
    if (staticContext === null)
      throw new Error("static scene coordinator canvas unavailable");
    const staticEpochStart = performance.now();
    const staticFrames: Array<
      Readonly<{
        frame: number;
        kind: "gap" | "image";
        timelineSeconds: number;
        activeAudioOwners: number;
        painted: boolean;
      }>
    > = [];
    let lastStaticFrame = -1;
    await new Promise<void>((resolve) => {
      const advance = (now: number) => {
        const timelineSeconds = (now - staticEpochStart) / 1000;
        const frame = Math.min(5, Math.floor(timelineSeconds * 24));
        if (frame !== lastStaticFrame) {
          const kind = frame < 3 ? "gap" : "image";
          const painted = kind === "image";
          if (painted) {
            staticContext.fillStyle = "rgb(16, 64, 128)";
            staticContext.fillRect(
              0,
              0,
              staticCanvas.width,
              staticCanvas.height,
            );
          } else {
            staticContext.fillStyle = "black";
            staticContext.fillRect(
              0,
              0,
              staticCanvas.width,
              staticCanvas.height,
            );
          }
          staticFrames.push({
            frame,
            kind,
            timelineSeconds,
            activeAudioOwners: activeNodes.length,
            painted,
          });
          lastStaticFrame = frame;
        }
        if (frame >= 5) resolve();
        else requestAnimationFrame(advance);
      };
      requestAnimationFrame(advance);
    });

    startOwner("owner-seek-before", 0.25);
    await new Promise((resolve) => setTimeout(resolve, 5));
    startOwner("owner-seek-after", 0.5);
    await new Promise((resolve) => setTimeout(resolve, 5));
    startOwner("owner-replacement", 0.75);
    await new Promise((resolve) => setTimeout(resolve, 5));

    const stateChanges: string[] = [];
    context.addEventListener("statechange", () => {
      stateChanges.push(context.state);
      if (context.state === "suspended") cancelOwner("context-suspended");
    });
    await context.suspend();
    await new Promise((resolve) => setTimeout(resolve, 60));
    await context.resume();
    startOwner("owner-after-resume", 0.25);
    await new Promise((resolve) => setTimeout(resolve, 5));
    cancelOwner("close");
    await context.close();
    ownedBuffer = null;
    retainedBytesAfterClose = ownedBufferBytes();
    await new Promise((resolve) => setTimeout(resolve, 60));

    return {
      schema: "M25-56AudioContextOwnerLifecyclePrototypeV1", // pragma: allowlist secret
      source: {
        ...sourceMetadata,
        decodedBytes: retainedDecodedBytes,
        admittedInPoints: events
          .filter((event) => event.reason === "start")
          .map((event) => event.inPointSeconds),
      },
      clockIntervals: {
        source: "performance.now",
        expectedOutputFrames: [0, 1, 2, 3, 4, 5],
        observedOutputFrames: staticFrames.map((sample) => sample.frame),
        scenes: staticFrames,
        monotonicAcrossGap: staticFrames
          .filter((sample) => sample.kind === "gap")
          .every(
            (sample, index, rows) =>
              index === 0 || sample.frame > rows[index - 1]!.frame,
          ),
        monotonicAcrossImage: staticFrames
          .filter((sample) => sample.kind === "image")
          .every(
            (sample, index, rows) =>
              index === 0 || sample.frame > rows[index - 1]!.frame,
          ),
        monotonicAcrossBoundary: staticFrames.every(
          (sample, index) =>
            index === 0 || sample.frame > staticFrames[index - 1]!.frame,
        ),
      },
      lifecycle: {
        events,
        stateChanges,
        callbackCommits,
        callbacksAfterCancellation,
        maximumActiveNodes,
        activeNodesAfterClose: activeNodes.length,
        retainedBytesAfterClose,
        finalContextState: context.state,
      },
      budgets: {
        activeAudioOwners: 1,
        prewarmAudioOwners: 0,
        decodedBytes: retainedDecodedBytes,
      },
    };
  });
  await testInfo.attach("m25-56-audio-context-owner-lifecycle", {
    contentType: "application/json",
    body: Buffer.from(JSON.stringify(evidence)),
  });
  console.log(`M25-56_AUDIO_CONTEXT_LIFECYCLE=${JSON.stringify(evidence)}`);
  expect(evidence.source).toMatchObject({
    sampleRate: 48_000,
    channels: 2,
    frames: 48_000,
  });
  expect(evidence.source.admittedInPoints).toEqual([
    0.25, 0.25, 0.5, 0.75, 0.25,
  ]);
  expect(evidence.clockIntervals.monotonicAcrossGap).toBe(true);
  expect(evidence.clockIntervals.monotonicAcrossImage).toBe(true);
  expect(evidence.clockIntervals.monotonicAcrossBoundary).toBe(true);
  expect(evidence.clockIntervals.observedOutputFrames).toEqual(
    evidence.clockIntervals.expectedOutputFrames,
  );
  expect(
    evidence.clockIntervals.scenes.every(
      (sample) => sample.activeAudioOwners === 0,
    ),
  ).toBe(true);
  expect(
    evidence.clockIntervals.scenes
      .filter((sample) => sample.kind === "gap")
      .every((sample) => sample.painted === false),
  ).toBe(true);
  expect(
    evidence.clockIntervals.scenes
      .filter((sample) => sample.kind === "image")
      .every((sample) => sample.painted === true),
  ).toBe(true);
  expect(evidence.lifecycle.stateChanges).toContain("suspended");
  expect(evidence.lifecycle.callbackCommits).toBe(1);
  expect(evidence.lifecycle.callbacksAfterCancellation).toBe(0);
  expect(evidence.lifecycle.maximumActiveNodes).toBe(1);
  expect(evidence.lifecycle.activeNodesAfterClose).toBe(0);
  expect(evidence.lifecycle.retainedBytesAfterClose).toBe(0);
  expect(evidence.lifecycle.finalContextState).toBe("closed");
});

test("M25-56 process loopback resolves a known 440 Hz tone and silence", async ({
  context,
  page,
}, testInfo) => {
  test.skip(
    process.platform !== "win32",
    "WASAPI process loopback is Windows-only",
  );
  await page.goto("/nleShell.html");
  const browser = context.browser();
  if (!browser) throw new Error("owned Chromium browser required");
  const cdp = await browser.newBrowserCDPSession();
  let browserPid: number | undefined;
  try {
    const processInfo = await cdp.send("SystemInfo.getProcessInfo");
    browserPid = processInfo.processInfo.find(
      (row) => row.type === "browser",
    )?.id;
  } finally {
    await cdp.detach();
  }
  if (!Number.isInteger(browserPid) || !browserPid || browserPid < 1)
    throw new Error("owned browser process identity missing");

  const observer = await startProcessAudioObserver(
    repositoryRoot,
    browserPid,
    processAudioObserverPath,
    processAudioObserverSha256,
  );
  try {
    const started = await page.evaluate(async () => {
      const audio = new AudioContext({ sampleRate: 48_000 });
      await audio.resume();
      const oscillator = audio.createOscillator();
      const gain = audio.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(440, audio.currentTime);
      gain.gain.setValueAtTime(0.12, audio.currentTime);
      oscillator.connect(gain).connect(audio.destination);
      oscillator.start();
      (
        window as typeof window & {
          __m25_56_audio_control?: {
            audio: AudioContext;
            oscillator: OscillatorNode;
            gain: GainNode;
          };
        }
      ).__m25_56_audio_control = { audio, oscillator, gain };
      return { state: audio.state, sampleRate: audio.sampleRate };
    });
    expect(started).toEqual({ state: "running", sampleRate: 48_000 });
    await page.waitForTimeout(900);
    await page.evaluate(async () => {
      const control = (
        window as typeof window & {
          __m25_56_audio_control?: {
            audio: AudioContext;
            oscillator: OscillatorNode;
            gain: GainNode;
          };
        }
      ).__m25_56_audio_control;
      if (!control) throw new Error("known audio control was not started");
      const stopAt = control.audio.currentTime + 0.03;
      control.gain.gain.setValueAtTime(
        control.gain.gain.value,
        control.audio.currentTime,
      );
      control.gain.gain.linearRampToValueAtTime(0, stopAt + 0.05);
      control.oscillator.stop(stopAt + 0.05);
      await new Promise<void>((resolve) => setTimeout(resolve, 130));
      await control.audio.close();
      delete (
        window as typeof window & {
          __m25_56_audio_control?: unknown;
        }
      ).__m25_56_audio_control;
    });
    await page.waitForTimeout(350);
    const capture = await observer.stop();
    expect(capture.rawAudioRetained).toBe(false);
    expect(capture.microphoneOpened).toBe(false);
    expect(capture.metrics.window_samples).toBe(480);
    expect(capture.metrics.samples_analyzed).toBeGreaterThan(48_000);
    const toneWindows = capture.metrics.envelopes.filter((envelope) =>
      envelope.channel_frequency_hz.every(
        (frequency, channel) =>
          Math.abs(frequency - 440) <= 1 &&
          envelope.channel_rms[channel]! >= 0.04,
      ),
    );
    expect(
      toneWindows.length,
      JSON.stringify({
        samplesAnalyzed: capture.metrics.samples_analyzed,
        windowSamples: capture.metrics.window_samples,
        envelopeCount: capture.metrics.envelopes.length,
        firstEnvelopes: capture.metrics.envelopes.slice(0, 12).map((row) => ({
          rms: row.rms,
          peak: row.peak,
          channelRms: row.channel_rms,
          channelFrequencyHz: row.channel_frequency_hz,
        })),
        maxEnvelopeRms: Math.max(
          0,
          ...capture.metrics.envelopes.map((row) => row.rms),
        ),
        onsetCount: capture.onsets.length,
        silentPackets: capture.silentPackets,
        silenceRuns: capture.metrics.silence_runs.slice(0, 4),
      }),
    ).toBeGreaterThanOrEqual(30);
    for (const channel of [0, 1] as const) {
      expect(
        capture.metrics.silence_runs.some(
          (run) =>
            run.channel === channel &&
            run.duration_samples >= 960 &&
            run.start_sample + run.duration_samples >=
              capture.metrics.samples_analyzed - 480,
        ),
      ).toBe(true);
    }
    const channelMedianFrequency = ([0, 1] as const).map((channel) => {
      const values = toneWindows
        .map((envelope) => envelope.channel_frequency_hz[channel])
        .sort((left, right) => left - right);
      return values[Math.floor(values.length / 2)] ?? 0;
    });
    const channelMeanRms = ([0, 1] as const).map(
      (channel) =>
        toneWindows.reduce(
          (total, envelope) => total + envelope.channel_rms[channel],
          0,
        ) / toneWindows.length,
    );
    const calibration = {
      schema: "M25-56AudioObserverCalibrationV1",
      observerSha256: processAudioObserverSha256,
      browserExecutableSha256: capture.browserExecutableSha256,
      selector: capture.selector,
      sampleRate: capture.sampleRate,
      windowSamples: capture.metrics.window_samples,
      packetTimeline: capture.packetTimeline,
      packetTimelineBreaks: summarizeTone(capture).packetTimelineBreakSamples,
      packetClockSamples: summarizeTone(capture).packetClockSamples,
      qpcBreakSamples: summarizeTone(capture).qpcBreakSamples,
      samplesAnalyzed: capture.metrics.samples_analyzed,
      envelopeCount: capture.metrics.envelopes.length,
      qualifyingToneWindows: toneWindows.length,
      channelMedianFrequencyHz: channelMedianFrequency,
      channelMeanRms,
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
    const calibrationPath = testInfo.outputPath(
      "m25-56-audio-observer-calibration.json",
    );
    await writeFile(calibrationPath, JSON.stringify(calibration, null, 2));
    await testInfo.attach("m25-56-audio-observer-calibration", {
      contentType: "application/json",
      path: calibrationPath,
    });
    console.log(
      `M25-56_AUDIO_OBSERVER=${JSON.stringify({
        observerSha256: processAudioObserverSha256,
        browserExecutableSha256: capture.browserExecutableSha256,
        selector: capture.selector,
        sampleRate: capture.sampleRate,
        windowSamples: capture.metrics.window_samples,
        packetTimeline: capture.packetTimeline,
        packetTimelineBreaks: summarizeTone(capture).packetTimelineBreakSamples,
        packetClockSamples: summarizeTone(capture).packetClockSamples,
        qpcBreakSamples: summarizeTone(capture).qpcBreakSamples,
        samplesAnalyzed: capture.metrics.samples_analyzed,
        qualifyingToneWindows: toneWindows.length,
        channelMedianFrequencyHz: channelMedianFrequency,
        channelMeanRms,
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
      })}`,
    );
  } finally {
    await observer.stop();
  }
});
