// M25-64 (rows #26 and #28): the output profile's facts, read once for the monitor header and the
// inspector's Project settings. The codec types the snapshot's `output` as
// `Record<string, unknown>`, so this is the one typed reader of it.
//
// IMPORTANT: a fact the output does not carry is omitted, never defaulted. The header and the
// Project settings state what the accepted snapshot says; a missing rate shown as "24 fps" would be
// a claim about the export the product cannot back. `nominalFps` keeps its 24 default only for the
// sidebar summary's timecode, which needs some rate to count in.

import { fill, nleCopy } from "../components/nle/nleCopy";
import type { Locale } from "../i18n/catalog";
import { formatTimelineTimecode } from "./timelineGeometry";

type Output = Readonly<Record<string, unknown>> | undefined;

/** The whole-number rate the output counts frames in, or null when it carries no valid rate. */
export function nominalRate(value: unknown): number | null {
  const rate = value as { num?: unknown; den?: unknown } | undefined;
  const num = Number(rate?.num);
  const den = Number(rate?.den);
  const fps = Math.round(num / den);
  return Number.isSafeInteger(fps) && fps > 0 ? fps : null;
}

/** The sidebar summary's counting rate: the output's, or 24 when it has none. */
export function nominalFps(value: unknown): number {
  return nominalRate(value) ?? 24;
}

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0
    ? value
    : null;
}

// Display names of the codec's closed values. An unknown value is omitted rather than shown raw:
// a machine token is not product copy.
const CODEC_NAMES: Readonly<Record<string, string>> = {
  aac: "AAC",
  h264: "H.264",
  mp4: "MP4",
};

function codecName(value: unknown): string | null {
  return typeof value === "string" ? (CODEC_NAMES[value] ?? null) : null;
}

function kilohertz(sampleRate: number): string {
  // 48000 -> "48", 44100 -> "44.1": exact decimal, no float formatting.
  const whole = Math.floor(sampleRate / 1000);
  const fraction = String(sampleRate % 1000)
    .padStart(3, "0")
    .replace(/0+$/u, "");
  return fraction === "" ? String(whole) : `${whole}.${fraction}`;
}

/** `W × H · N fps` for the monitor header, or null when the output states neither fact. */
export function monitorHeaderFacts(
  locale: Locale,
  output: Output,
): string | null {
  const copy = nleCopy(locale).project;
  const width = positiveInteger(output?.width);
  const height = positiveInteger(output?.height);
  const fps = nominalRate(output?.frameRate);
  const parts = [
    width !== null && height !== null
      ? fill(copy.size, { width, height })
      : null,
    fps !== null ? fill(copy.fps, { fps }) : null,
  ].filter((part): part is string => part !== null);
  return parts.length === 0 ? null : parts.join(" · ");
}

export type ProjectSettingKey =
  "resolution" | "frameRate" | "duration" | "audio" | "export";

export type ProjectSetting = Readonly<{
  key: ProjectSettingKey;
  label: string;
  value: string;
  /** Timecodes are set in the monospace face, as in the canvas. */
  mono: boolean;
}>;

/** The Project settings rows, in the canvas's order, each only when the output states it. */
export function projectSettings(
  locale: Locale,
  output: Output,
): readonly ProjectSetting[] {
  const copy = nleCopy(locale).project;
  const rows: ProjectSetting[] = [];
  const width = positiveInteger(output?.width);
  const height = positiveInteger(output?.height);
  if (width !== null && height !== null)
    rows.push({
      key: "resolution",
      label: copy.resolution,
      value: fill(copy.size, { width, height }),
      mono: false,
    });
  const fps = nominalRate(output?.frameRate);
  if (fps !== null)
    rows.push({
      key: "frameRate",
      label: copy.frameRate,
      value: fill(copy.fps, { fps }),
      mono: false,
    });
  // Duration is the output's own frame count (M25-57 may later change what that count covers);
  // a timecode needs the rate it counts in, so both must be present.
  const frames = output?.durationFrames;
  if (
    fps !== null &&
    typeof frames === "number" &&
    Number.isSafeInteger(frames) &&
    frames >= 0
  )
    rows.push({
      key: "duration",
      label: copy.duration,
      value: formatTimelineTimecode(frames, fps),
      mono: true,
    });
  const channels = positiveInteger(output?.channels);
  const sampleRate = positiveInteger(output?.sampleRate);
  const audio = [
    codecName(output?.audioCodec),
    sampleRate !== null
      ? fill(copy.kilohertz, { rate: kilohertz(sampleRate) })
      : null,
    channels === null
      ? null
      : channels === 1
        ? copy.mono
        : channels === 2
          ? copy.stereo
          : fill(copy.channels, { count: channels }),
  ].filter((part): part is string => part !== null);
  if (audio.length > 0)
    rows.push({
      key: "audio",
      label: copy.audio,
      value: audio.join(" · "),
      mono: false,
    });
  const format = [
    codecName(output?.container),
    codecName(output?.videoCodec),
  ].filter((part): part is string => part !== null);
  if (format.length > 0)
    rows.push({
      key: "export",
      label: copy.export,
      value: format.join(" · "),
      mono: false,
    });
  return rows;
}
