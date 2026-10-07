import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

type ProbeStream = Readonly<Record<string, unknown>>;

export type IntegratedOutputObservation = Readonly<{
  schema: "h3.context.integrated_output_observation.v1";
  outputSha256: string;
  outputBytes: number;
  observer: Readonly<{
    ffmpegSha256: string;
    ffprobeSha256: string;
  }>;
  video: Readonly<{
    frames: number;
    rate: string;
    width: number;
    height: number;
    durationSeconds: number;
  }>;
  audio: Readonly<{
    streams: number;
    sampleRate: number;
    channels: number;
    decodedChannels: 1;
    decodedSampleRate: 48_000;
    decodedSamples: number;
    decodedBytes: number;
    packetCount: number;
    firstPacketPtsSeconds: number;
    lastPacketPtsSeconds: number;
    maximumPacketGapMs: number;
    maximumSilentRunSamplesAroundCut: number;
    rms: Readonly<{
      opening: number;
      beforeCut: number;
      afterCut: number;
      tail: number;
    }>;
  }>;
}>;

const sha256 = (body: Uint8Array) =>
  createHash("sha256").update(body).digest("hex");

function executable(variable: string): string {
  const value = process.env[variable];
  if (!value) throw new Error(`${variable} must name the authorized observer`);
  return value;
}

function number(value: unknown, label: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${label} is not finite`);
  return parsed;
}

function rms(samples: Int16Array, start: number, count = 4_800): number {
  const lower = Math.max(0, Math.min(samples.length, Math.round(start)));
  const upper = Math.min(samples.length, lower + count);
  if (upper <= lower) throw new Error("audio observation window is empty");
  let sum = 0;
  for (let index = lower; index < upper; index += 1) {
    const value = samples[index]! / 32_768;
    sum += value * value;
  }
  return Math.sqrt(sum / (upper - lower));
}

function maximumSilentRun(
  samples: Int16Array,
  center: number,
  radius = 4_800,
): number {
  const lower = Math.max(0, Math.round(center) - radius);
  const upper = Math.min(samples.length, Math.round(center) + radius);
  let current = 0;
  let maximum = 0;
  for (let index = lower; index < upper; index += 1) {
    if (Math.abs(samples[index]!) <= 32) {
      current += 1;
      maximum = Math.max(maximum, current);
    } else current = 0;
  }
  return maximum;
}

export function observeIntegratedOutput(
  path: string,
  expectedFrames: number,
  cutFrame: number,
): IntegratedOutputObservation {
  const ffmpeg = executable("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH");
  const ffprobe = executable("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH");
  const bytes = readFileSync(path);
  const probed = JSON.parse(
    execFileSync(
      ffprobe,
      [
        "-v",
        "error",
        "-count_frames",
        "-show_entries",
        "stream=codec_type,width,height,nb_read_frames,r_frame_rate,sample_rate,channels,duration:format=duration",
        "-of",
        "json",
        path,
      ],
      { encoding: "utf8", timeout: 120_000 },
    ),
  ) as { streams: ProbeStream[]; format: Record<string, unknown> };
  const video = probed.streams.find((stream) => stream.codec_type === "video");
  const audioStreams = probed.streams.filter(
    (stream) => stream.codec_type === "audio",
  );
  if (!video || audioStreams.length !== 1)
    throw new Error("output must contain one video and one audio stream");
  const audio = audioStreams[0]!;
  const packets = JSON.parse(
    execFileSync(
      ffprobe,
      [
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,duration_time,size",
        "-of",
        "json",
        path,
      ],
      { encoding: "utf8", timeout: 120_000, maxBuffer: 2 * 1024 * 1024 },
    ),
  ) as { packets: Array<Record<string, unknown>> };
  if (packets.packets.length === 0)
    throw new Error("audio packet timeline is empty");
  const packetPts = packets.packets.map((packet, index) =>
    number(packet.pts_time, `audio packet ${index} pts`),
  );
  const packetDurations = packets.packets.map((packet, index) =>
    number(packet.duration_time, `audio packet ${index} duration`),
  );
  for (let index = 1; index < packetPts.length; index += 1)
    if (packetPts[index]! < packetPts[index - 1]!)
      throw new Error("audio packet timestamps are not monotonic");
  if (packetDurations.some((duration) => duration <= 0))
    throw new Error("audio packet duration must stay positive");
  const maximumPacketGapMs = packetPts
    .slice(1)
    .reduce(
      (maximum, pts, index) =>
        Math.max(
          maximum,
          Math.max(0, pts - (packetPts[index]! + packetDurations[index]!)) *
            1_000,
        ),
      0,
    );
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
  if (pcm.byteLength === 0 || pcm.byteLength > 2 * 1024 * 1024)
    throw new Error("decoded audio exceeded the bounded observer envelope");
  const samples = new Int16Array(
    pcm.buffer,
    pcm.byteOffset,
    Math.floor(pcm.byteLength / Int16Array.BYTES_PER_ELEMENT),
  );
  const cutSample = (cutFrame * 48_000) / 24;
  const durationSamples = (expectedFrames * 48_000) / 24;
  const windows = {
    opening: rms(samples, 0.4 * 48_000),
    beforeCut: rms(samples, cutSample - 0.2 * 48_000),
    afterCut: rms(samples, cutSample + 0.2 * 48_000),
    tail: rms(samples, durationSamples - 0.5 * 48_000),
  };
  return Object.freeze({
    schema: "h3.context.integrated_output_observation.v1",
    outputSha256: sha256(bytes),
    outputBytes: bytes.byteLength,
    observer: Object.freeze({
      ffmpegSha256: sha256(readFileSync(ffmpeg)),
      ffprobeSha256: sha256(readFileSync(ffprobe)),
    }),
    video: Object.freeze({
      frames: number(video.nb_read_frames, "video frame count"),
      rate: String(video.r_frame_rate),
      width: number(video.width, "video width"),
      height: number(video.height, "video height"),
      durationSeconds: number(probed.format.duration, "container duration"),
    }),
    audio: Object.freeze({
      streams: audioStreams.length,
      sampleRate: number(audio.sample_rate, "audio sample rate"),
      channels: number(audio.channels, "audio channels"),
      decodedChannels: 1,
      decodedSampleRate: 48_000,
      decodedSamples: samples.length,
      decodedBytes: pcm.byteLength,
      packetCount: packets.packets.length,
      firstPacketPtsSeconds: packetPts[0]!,
      lastPacketPtsSeconds: packetPts.at(-1)!,
      maximumPacketGapMs,
      maximumSilentRunSamplesAroundCut: maximumSilentRun(samples, cutSample),
      rms: Object.freeze(windows),
    }),
  });
}
