import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { readFile, realpath, stat } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";

const MAX_STDOUT_BYTES = 2_000_000;
const MAX_STDERR_BYTES = 64_000;
const MAX_PACKETS = 5_000;
const MAX_EVENTS = 5_000;
const MAX_ENVELOPES = 4_501;
const MAX_SAMPLES = 45 * 48_000;

export type AudioMetricRun = Readonly<{
  channel: 0 | 1;
  start_sample: number;
  duration_samples: number;
}>;

export type AudioImpulse = Readonly<{
  channel: 0 | 1;
  start_sample: number;
  duration_samples: number;
  peak: number;
}>;

export type AudioEnvelope = Readonly<{
  start_sample: number;
  sample_count: number;
  peak: number;
  rms: number;
  frequency_hz: number;
  clipped_samples: number;
  channel_rms: readonly [number, number];
  channel_frequency_hz: readonly [number, number];
  channel_clipped_samples: readonly [number, number];
}>;

export type AudioCapture = Readonly<{
  schema: "h3.context.process_audio_observer_capture.v3";
  selector: "include_test_browser_process_tree";
  sampleRate: 48_000;
  observationFormat: "pcm_float32_stereo";
  browserExecutableSha256: string;
  packets: Array<{
    time: number;
    sampleStart: number;
    devicePosition: number;
    positionDeltaFrames: number | null;
    qpcPosition: number;
    qpcDelta100ns: number | null;
    qpcError100ns: number | null;
    timelineGapFrames: number;
    frames: number;
    peak: number;
    pcm16Peak: number;
    flags: number;
    clockBracketNs: number;
  }>;
  onsets: Array<{ time: number }>;
  metrics: Readonly<{
    sample_rate: 48_000;
    channels: 2;
    samples_analyzed: number;
    window_samples: 480 | 960;
    silence_floor: number;
    silence_runs: AudioMetricRun[];
    impulse_floor: number;
    impulse_events: AudioImpulse[];
    envelopes: AudioEnvelope[];
    raw_audio_retained: false;
  }>;
  discontinuityPackets: number;
  timestampErrorPackets: number;
  silentPackets: number;
  packetTimeline: Readonly<{
    clockSource: "device_position" | "qpc" | "unavailable";
    valid: boolean;
    positionBreaks: number;
    positionGaps: number;
    positionOverlaps: number;
    discontinuityPackets: number;
    timestampErrorPackets: number;
    qpcValid: boolean;
    qpcBreaks: number;
    qpcGaps: number;
    qpcOverlaps: number;
    qpcGapFramesFilled: number;
  }>;
  rawAudioRetained: false;
  microphoneOpened: false;
}>;

type RecordValue = Record<string, unknown>;

function record(value: unknown): RecordValue | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as RecordValue)
    : null;
}

function finite(
  value: unknown,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
) {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= minimum &&
    value <= maximum
  );
}

function boundedArray(value: unknown, maximum: number): value is unknown[] {
  return Array.isArray(value) && value.length <= maximum;
}

function finitePair(
  value: unknown,
  minimum: number,
  maximum: number,
): value is [number, number] {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    value.every((item) => finite(item, minimum, maximum))
  );
}

function sampleChannel(value: unknown): value is 0 | 1 {
  return Number.isInteger(value) && (value === 0 || value === 1);
}

export function decodeAudioObserverReady(value: unknown): string {
  const ready = record(value);
  if (
    ready?.ready !== true ||
    ready.selector !== "include_test_browser_process_tree" ||
    typeof ready.browserExecutableSha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(ready.browserExecutableSha256)
  )
    throw new Error("audio observer process identity invalid");
  return ready.browserExecutableSha256;
}

export function decodeAudioCapture(
  value: unknown,
  expectedBrowserHash: string,
): AudioCapture {
  const capture = record(value);
  const metrics = record(capture?.metrics);
  const packetTimeline = record(capture?.packetTimeline);
  if (
    typeof capture?.browserExecutableSha256 === "string" &&
    capture.browserExecutableSha256 !== expectedBrowserHash
  )
    throw new Error("audio observer process identity changed");
  if (
    capture?.schema !== "h3.context.process_audio_observer_capture.v3" ||
    capture.selector !== "include_test_browser_process_tree" ||
    capture.sampleRate !== 48_000 ||
    capture.observationFormat !== "pcm_float32_stereo" ||
    capture.browserExecutableSha256 !== expectedBrowserHash ||
    capture.rawAudioRetained !== false ||
    capture.microphoneOpened !== false ||
    !metrics ||
    metrics.sample_rate !== 48_000 ||
    metrics.channels !== 2 ||
    metrics.raw_audio_retained !== false ||
    !finite(metrics.samples_analyzed, 0, MAX_SAMPLES) ||
    ![480, 960].includes(Number(metrics.window_samples)) ||
    !finite(metrics.silence_floor, 0, 1) ||
    !finite(metrics.impulse_floor, 0, 1) ||
    !boundedArray(capture.packets, MAX_PACKETS) ||
    !boundedArray(capture.onsets, MAX_PACKETS) ||
    !boundedArray(metrics.silence_runs, MAX_EVENTS) ||
    !boundedArray(metrics.impulse_events, MAX_EVENTS) ||
    !boundedArray(metrics.envelopes, MAX_ENVELOPES) ||
    !finite(capture.discontinuityPackets, 0, MAX_PACKETS) ||
    !finite(capture.timestampErrorPackets, 0, MAX_PACKETS) ||
    !finite(capture.silentPackets, 0, MAX_PACKETS) ||
    !packetTimeline ||
    !["device_position", "qpc", "unavailable"].includes(
      String(packetTimeline.clockSource),
    ) ||
    typeof packetTimeline.valid !== "boolean" ||
    !finite(packetTimeline.positionBreaks, 0, MAX_PACKETS) ||
    !Number.isSafeInteger(packetTimeline.positionBreaks) ||
    !finite(packetTimeline.positionGaps, 0, MAX_PACKETS) ||
    !Number.isSafeInteger(packetTimeline.positionGaps) ||
    !finite(packetTimeline.positionOverlaps, 0, MAX_PACKETS) ||
    !Number.isSafeInteger(packetTimeline.positionOverlaps) ||
    !finite(packetTimeline.discontinuityPackets, 0, MAX_PACKETS) ||
    !Number.isSafeInteger(packetTimeline.discontinuityPackets) ||
    !finite(packetTimeline.timestampErrorPackets, 0, MAX_PACKETS) ||
    !Number.isSafeInteger(packetTimeline.timestampErrorPackets) ||
    typeof packetTimeline.qpcValid !== "boolean" ||
    !finite(packetTimeline.qpcBreaks, 0, MAX_PACKETS) ||
    !Number.isSafeInteger(packetTimeline.qpcBreaks) ||
    !finite(packetTimeline.qpcGaps, 0, MAX_PACKETS) ||
    !Number.isSafeInteger(packetTimeline.qpcGaps) ||
    !finite(packetTimeline.qpcOverlaps, 0, MAX_PACKETS) ||
    !Number.isSafeInteger(packetTimeline.qpcOverlaps) ||
    !finite(packetTimeline.qpcGapFramesFilled, 0, MAX_SAMPLES) ||
    !Number.isSafeInteger(packetTimeline.qpcGapFramesFilled) ||
    packetTimeline.positionBreaks !==
      Number(packetTimeline.positionGaps) +
        Number(packetTimeline.positionOverlaps) ||
    packetTimeline.qpcBreaks !==
      Number(packetTimeline.qpcGaps) + Number(packetTimeline.qpcOverlaps) ||
    packetTimeline.discontinuityPackets !== capture.discontinuityPackets ||
    packetTimeline.timestampErrorPackets !== capture.timestampErrorPackets
  )
    throw new Error("audio observer invalid output");

  let expectedSampleStart = 0;
  for (const raw of capture.packets) {
    const packet = record(raw);
    if (
      !packet ||
      !finite(packet.time) ||
      !finite(packet.sampleStart, 0, MAX_SAMPLES) ||
      !finite(packet.devicePosition, 0, Number.MAX_SAFE_INTEGER) ||
      !Number.isSafeInteger(packet.devicePosition) ||
      !finite(packet.qpcPosition, 0, Number.MAX_SAFE_INTEGER) ||
      !Number.isSafeInteger(packet.qpcPosition) ||
      !(
        packet.positionDeltaFrames === null ||
        (typeof packet.positionDeltaFrames === "number" &&
          Number.isSafeInteger(packet.positionDeltaFrames) &&
          Math.abs(packet.positionDeltaFrames) <= Number.MAX_SAFE_INTEGER)
      ) ||
      !(
        packet.qpcDelta100ns === null ||
        (typeof packet.qpcDelta100ns === "number" &&
          Number.isSafeInteger(packet.qpcDelta100ns) &&
          Math.abs(packet.qpcDelta100ns) <= Number.MAX_SAFE_INTEGER)
      ) ||
      !(
        packet.qpcError100ns === null ||
        finite(
          packet.qpcError100ns,
          -Number.MAX_SAFE_INTEGER,
          Number.MAX_SAFE_INTEGER,
        )
      ) ||
      !finite(packet.timelineGapFrames, 0, MAX_SAMPLES) ||
      !Number.isSafeInteger(packet.timelineGapFrames) ||
      !finite(packet.frames, 0, 48_000) ||
      !finite(packet.peak, 0, 1) ||
      !finite(packet.pcm16Peak, 0, 32_768) ||
      !finite(packet.flags, 0, 0xffff_ffff) ||
      !finite(packet.clockBracketNs, 0, 1_000_000_000)
    )
      throw new Error("audio observer invalid output");
    if (
      Number(packet.sampleStart) !==
      expectedSampleStart + Number(packet.timelineGapFrames)
    )
      throw new Error("audio observer invalid output");
    expectedSampleStart = Number(packet.sampleStart) + Number(packet.frames);
  }
  if (
    capture.packets.reduce<number>(
      (total, raw) => total + Number(record(raw)?.timelineGapFrames ?? 0),
      0,
    ) !== packetTimeline.qpcGapFramesFilled ||
    (packetTimeline.clockSource !== "qpc" &&
      packetTimeline.qpcGapFramesFilled !== 0)
  )
    throw new Error("audio observer invalid output");
  if (expectedSampleStart !== metrics.samples_analyzed)
    throw new Error("audio observer invalid output");
  for (const raw of capture.onsets) {
    const onset = record(raw);
    if (!onset || !finite(onset.time))
      throw new Error("audio observer invalid output");
  }
  for (const raw of metrics.silence_runs) {
    const run = record(raw);
    if (
      !run ||
      !sampleChannel(run.channel) ||
      !finite(run.start_sample, 0, MAX_SAMPLES) ||
      !finite(run.duration_samples, 1, MAX_SAMPLES) ||
      Number(run.start_sample) + Number(run.duration_samples) >
        Number(metrics.samples_analyzed)
    )
      throw new Error("audio observer invalid output");
  }
  for (const raw of metrics.impulse_events) {
    const event = record(raw);
    if (
      !event ||
      !sampleChannel(event.channel) ||
      !finite(event.start_sample, 0, MAX_SAMPLES) ||
      !finite(event.duration_samples, 1, 480) ||
      !finite(event.peak, 0, 1) ||
      Number(event.start_sample) + Number(event.duration_samples) >
        Number(metrics.samples_analyzed)
    )
      throw new Error("audio observer invalid output");
  }
  for (const raw of metrics.envelopes) {
    const envelope = record(raw);
    if (
      !envelope ||
      !finite(envelope.start_sample, 0, MAX_SAMPLES) ||
      !finite(envelope.sample_count, 1, 960) ||
      !finite(envelope.peak, 0, 1) ||
      !finite(envelope.rms, 0, 1) ||
      !finite(envelope.frequency_hz, 0, 24_000) ||
      !finite(envelope.clipped_samples, 0, 960) ||
      !finitePair(envelope.channel_rms, 0, 1) ||
      !finitePair(envelope.channel_frequency_hz, 0, 24_000) ||
      !finitePair(envelope.channel_clipped_samples, 0, 960) ||
      Number(envelope.start_sample) + Number(envelope.sample_count) >
        Number(metrics.samples_analyzed)
    )
      throw new Error("audio observer invalid output");
  }
  return capture as unknown as AudioCapture;
}

export function audioPacketTimelineViolation(
  capture: AudioCapture,
): "audio_observer_packet_timeline_invalid" | null {
  const timeline = capture.packetTimeline;
  const clockValid =
    (timeline.clockSource === "device_position" &&
      timeline.positionBreaks === 0 &&
      timeline.positionGaps === 0 &&
      timeline.positionOverlaps === 0) ||
    (timeline.clockSource === "qpc" &&
      timeline.qpcValid &&
      timeline.qpcOverlaps === 0 &&
      (timeline.qpcGaps === 0 || timeline.qpcGapFramesFilled > 0));
  return timeline.valid &&
    clockValid &&
    timeline.positionBreaks === 0 &&
    timeline.positionGaps === 0 &&
    timeline.positionOverlaps === 0 &&
    timeline.discontinuityPackets === 0 &&
    timeline.timestampErrorPackets === 0 &&
    capture.discontinuityPackets === 0 &&
    capture.timestampErrorPackets === 0
    ? null
    : "audio_observer_packet_timeline_invalid";
}

/** Only the pinned process-loopback observer may inspect this test browser's audio. */
export async function startProcessAudioObserver(
  repositoryRoot: string,
  pid: number,
  configured: string,
  expectedHash: string,
) {
  if (!Number.isInteger(pid) || pid < 1 || !/^[a-f0-9]{64}$/.test(expectedHash))
    throw new Error("audio observer identity required");
  const observer = await realpath(configured);
  const relativePath = relative(await realpath(repositoryRoot), observer);
  if (isAbsolute(relativePath) || relativePath.startsWith(".."))
    throw new Error("audio observer outside project");
  const metadata = await stat(observer);
  if (
    !metadata.isFile() ||
    metadata.size > 64 * 1024 ||
    createHash("sha256")
      .update(await readFile(observer))
      .digest("hex") !== expectedHash
  )
    throw new Error("audio observer pin mismatch");
  const child = spawn(
    resolve(repositoryRoot, ".venv/Scripts/python.exe"),
    [observer, "--pid", String(pid), "--duration", "45"],
    { windowsHide: true },
  );
  let result: AudioCapture | null = null;
  let browserHash: string | null = null;
  let outputFailure: Error | null = null;
  let stdoutBytes = 0;
  let stderrBytes = 0;
  const exited = new Promise<void>((accept, reject) => {
    child.on("error", () => reject(new Error("audio observer start failed")));
    child.on("exit", (code) =>
      code === 0 ? accept() : reject(new Error("audio observer failed")),
    );
  });
  let rejectReady: (error: Error) => void = () => undefined;
  const ready = new Promise<void>((accept, reject) => {
    rejectReady = reject;
    let pending = "";
    child.stdout.on("data", (chunk: Buffer) => {
      stdoutBytes += chunk.length;
      if (stdoutBytes > MAX_STDOUT_BYTES) {
        outputFailure = new Error("audio observer output bound");
        child.kill();
        rejectReady(outputFailure);
        return;
      }
      if (outputFailure) return;
      pending += chunk.toString();
      let index: number;
      while ((index = pending.indexOf("\n")) >= 0) {
        try {
          const row = JSON.parse(pending.slice(0, index)) as RecordValue;
          if (row.failure) {
            const failureCode =
              typeof row.failure === "string" &&
              /^[a-z][a-z0-9_]{0,96}$/.test(row.failure)
                ? row.failure
                : "unknown";
            throw new Error(`audio observer unavailable: ${failureCode}`);
          }
          if (row.ready === true) {
            browserHash = decodeAudioObserverReady(row);
            accept();
          }
          if (row.result) {
            if (browserHash === null)
              throw new Error("audio observer result preceded readiness");
            result = decodeAudioCapture(row.result, browserHash);
          }
        } catch (error) {
          outputFailure =
            error instanceof Error
              ? error
              : new Error("audio observer invalid output");
          child.kill();
          rejectReady(outputFailure);
          return;
        }
        pending = pending.slice(index + 1);
      }
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderrBytes += chunk.length;
      if (stderrBytes > MAX_STDERR_BYTES) {
        outputFailure = new Error("audio observer stderr bound");
        child.kill();
        rejectReady(outputFailure);
      }
    });
    void exited.then(
      () => reject(new Error("audio observer ended before readiness")),
      reject,
    );
  });
  try {
    await ready;
  } catch (error) {
    child.stdin.end("stop\n");
    await exited.catch(() => undefined);
    throw outputFailure ?? error;
  }
  let stopTask: Promise<AudioCapture> | null = null;
  return {
    stop(): Promise<AudioCapture> {
      if (stopTask) return stopTask;
      stopTask = (async () => {
        if (child.exitCode === null) child.stdin.end("stop\n");
        try {
          await new Promise<void>((resolveExit, rejectExit) => {
            const timer = setTimeout(() => {
              child.kill();
              rejectExit(new Error("audio observer shutdown timeout"));
            }, 5_000);
            exited.then(
              () => {
                clearTimeout(timer);
                resolveExit();
              },
              (error: unknown) => {
                clearTimeout(timer);
                rejectExit(error);
              },
            );
          });
        } catch (error) {
          if (!outputFailure) throw error;
        }
        if (outputFailure) throw outputFailure;
        if (!result) throw new Error("audio observer result missing");
        return result;
      })();
      return stopTask;
    },
  };
}
