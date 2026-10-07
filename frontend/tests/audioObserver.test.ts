import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  audioPacketTimelineViolation,
  decodeAudioCapture,
  decodeAudioObserverReady,
  startProcessAudioObserver,
} from "./e2e/host/audioObserver";

const repositoryRoot = resolve(process.cwd(), "..");
const fakeObserver = resolve(
  repositoryRoot,
  "frontend/tests/fixtures/m25_56_audio_observer_fake.py",
);
const observerHash = createHash("sha256")
  .update(readFileSync(fakeObserver))
  .digest("hex");
const browserHash = "a".repeat(64);
const windowsIt = process.platform === "win32" ? it : it.skip;

function capture() {
  return {
    schema: "h3.context.process_audio_observer_capture.v3",
    selector: "include_test_browser_process_tree",
    sampleRate: 48_000,
    observationFormat: "pcm_float32_stereo",
    browserExecutableSha256: browserHash,
    packets: [],
    onsets: [],
    metrics: {
      sample_rate: 48_000,
      channels: 2,
      samples_analyzed: 0,
      window_samples: 480,
      silence_floor: 0.01,
      silence_runs: [],
      impulse_floor: 0.5,
      impulse_events: [],
      envelopes: [],
      raw_audio_retained: false,
    },
    discontinuityPackets: 0,
    timestampErrorPackets: 0,
    silentPackets: 0,
    packetTimeline: {
      clockSource: "device_position",
      valid: true,
      positionBreaks: 0,
      positionGaps: 0,
      positionOverlaps: 0,
      discontinuityPackets: 0,
      timestampErrorPackets: 0,
      qpcValid: true,
      qpcBreaks: 0,
      qpcGaps: 0,
      qpcOverlaps: 0,
      qpcGapFramesFilled: 0,
    },
    rawAudioRetained: false,
    microphoneOpened: false,
  };
}

describe("M25-56 process audio observer boundary", () => {
  it("requires an owned-browser readiness receipt", () => {
    expect(
      decodeAudioObserverReady({
        ready: true,
        selector: "include_test_browser_process_tree",
        browserExecutableSha256: browserHash,
      }),
    ).toBe(browserHash);
    expect(() =>
      decodeAudioObserverReady({
        ready: true,
        selector: "default_audio_endpoint",
        browserExecutableSha256: browserHash,
      }),
    ).toThrow("process identity invalid");
  });

  it("accepts bounded aggregate output and refuses malformed or over-bound output", () => {
    expect(
      decodeAudioCapture(capture(), browserHash).metrics.samples_analyzed,
    ).toBe(0);
    expect(() =>
      decodeAudioCapture({ ...capture(), metrics: undefined }, browserHash),
    ).toThrow("invalid output");
    expect(() =>
      decodeAudioCapture(
        { ...capture(), packets: Array.from({ length: 5_001 }, () => ({})) },
        browserHash,
      ),
    ).toThrow("invalid output");
  });

  it("never classifies audio with an invalid device-frame timeline as clean", () => {
    const invalid = {
      ...capture(),
      packetTimeline: {
        ...capture().packetTimeline,
        valid: false,
        positionGaps: 1,
        positionBreaks: 1,
      },
    };
    expect(
      audioPacketTimelineViolation(decodeAudioCapture(invalid, browserHash)),
    ).toBe("audio_observer_packet_timeline_invalid");
    expect(
      audioPacketTimelineViolation(decodeAudioCapture(capture(), browserHash)),
    ).toBeNull();
  });

  it("counts bounded QPC gaps as synthetic silence before classifying capture", () => {
    const filledGap = {
      ...capture(),
      packets: [
        {
          time: 0,
          sampleStart: 0,
          devicePosition: 0,
          positionDeltaFrames: null,
          qpcPosition: 10_000_000,
          qpcDelta100ns: null,
          qpcError100ns: null,
          timelineGapFrames: 0,
          frames: 480,
          peak: 0.2,
          pcm16Peak: 6_554,
          flags: 0,
          clockBracketNs: 100,
        },
        {
          time: 1,
          sampleStart: 1_440,
          devicePosition: 0,
          positionDeltaFrames: null,
          qpcPosition: 10_400_000,
          qpcDelta100ns: 300_000,
          qpcError100ns: 200_000,
          timelineGapFrames: 960,
          frames: 480,
          peak: 0.2,
          pcm16Peak: 6_554,
          flags: 0,
          clockBracketNs: 100,
        },
      ],
      metrics: {
        ...capture().metrics,
        samples_analyzed: 1_920,
      },
      packetTimeline: {
        ...capture().packetTimeline,
        clockSource: "qpc",
        valid: true,
        qpcBreaks: 1,
        qpcGaps: 1,
        qpcGapFramesFilled: 960,
      },
    };
    const decoded = decodeAudioCapture(filledGap, browserHash);
    expect(audioPacketTimelineViolation(decoded)).toBeNull();
    expect(() =>
      decodeAudioCapture(
        {
          ...filledGap,
          packetTimeline: {
            ...filledGap.packetTimeline,
            qpcGapFramesFilled: 0,
          },
        },
        browserHash,
      ),
    ).toThrow("invalid output");
  });

  it("requires channel-attributed runs and two-channel envelope facts", () => {
    const measuredEnvelope = {
      start_sample: 0,
      sample_count: 480,
      peak: 0.25,
      rms: 0.125,
      frequency_hz: 440,
      clipped_samples: 0,
      channel_rms: [0.1768, 0],
      channel_frequency_hz: [440, 0],
      channel_clipped_samples: [0, 0],
    };
    const base = capture();
    const measured = {
      ...base,
      packets: [
        {
          time: 0,
          sampleStart: 0,
          devicePosition: 0,
          positionDeltaFrames: null,
          qpcPosition: 0,
          qpcDelta100ns: null,
          qpcError100ns: null,
          timelineGapFrames: 0,
          frames: 480,
          peak: 0.25,
          pcm16Peak: 8_192,
          flags: 0,
          clockBracketNs: 0,
        },
      ],
      metrics: {
        ...base.metrics,
        samples_analyzed: 480,
        silence_runs: [{ channel: 1, start_sample: 0, duration_samples: 480 }],
        impulse_events: [
          { channel: 0, start_sample: 0, duration_samples: 48, peak: 0.9 },
        ],
        envelopes: [measuredEnvelope],
      },
    };

    expect(
      decodeAudioCapture(measured, browserHash).metrics.envelopes,
    ).toHaveLength(1);
    expect(() =>
      decodeAudioCapture(
        {
          ...measured,
          metrics: {
            ...measured.metrics,
            silence_runs: [{ start_sample: 0, duration_samples: 48 }],
          },
        },
        browserHash,
      ),
    ).toThrow("invalid output");
    expect(() =>
      decodeAudioCapture(
        {
          ...measured,
          metrics: {
            ...measured.metrics,
            envelopes: [
              {
                ...measuredEnvelope,
                channel_frequency_hz: [440],
              },
            ],
          },
        },
        browserHash,
      ),
    ).toThrow("invalid output");
  });

  it("checks the source pin before starting the observer process", async () => {
    await expect(
      startProcessAudioObserver(
        repositoryRoot,
        123,
        fakeObserver,
        "0".repeat(64),
      ),
    ).rejects.toThrow("pin mismatch");
  });

  windowsIt(
    "stops cleanly and returns only validated aggregate results",
    async () => {
      const observer = await startProcessAudioObserver(
        repositoryRoot,
        123,
        fakeObserver,
        observerHash,
      );
      const result = await observer.stop();
      expect(result.schema).toBe(
        "h3.context.process_audio_observer_capture.v3",
      );
      expect(result.rawAudioRetained).toBe(false);
    },
  );

  windowsIt(
    "keeps a bounded observer failure code for actionable diagnostics",
    async () => {
      await expect(
        startProcessAudioObserver(
          repositoryRoot,
          127,
          fakeObserver,
          observerHash,
        ),
      ).rejects.toThrow("process_loopback_activation_result_0x80004005");
    },
  );

  windowsIt(
    "rejects a changed process identity and stdout overflow",
    async () => {
      const mismatch = await startProcessAudioObserver(
        repositoryRoot,
        126,
        fakeObserver,
        observerHash,
      );
      await expect(mismatch.stop()).rejects.toThrow("process identity changed");

      const overflow = await startProcessAudioObserver(
        repositoryRoot,
        124,
        fakeObserver,
        observerHash,
      );
      await expect(overflow.stop()).rejects.toThrow("output bound");
    },
  );

  windowsIt(
    "rejects malformed final output after a successful handshake",
    async () => {
      const observer = await startProcessAudioObserver(
        repositoryRoot,
        125,
        fakeObserver,
        observerHash,
      );
      await expect(observer.stop()).rejects.toThrow("invalid output");
    },
  );
});
