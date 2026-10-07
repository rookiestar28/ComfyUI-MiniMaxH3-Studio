// M25-64 (rows #26 and #28): the monitor header and the inspector's Project settings read the
// accepted output profile through one reader. Every fact comes from the output; an absent or
// malformed one is omitted, never defaulted, and no codec token reaches a reader.

import { describe, expect, it } from "vitest";

import type { Locale } from "../src/i18n/catalog";
import {
  monitorHeaderFacts,
  nominalFps,
  nominalRate,
  projectSettings,
} from "../src/runtime/nleProjectSettings";
import { SMOKE_SHAPE, snapshotFixture } from "./support/nleWorkspaceFixture";

const LOCALES: readonly Locale[] = ["en", "zh-TW", "zh-CN"];
const MACHINE_TOKEN = /\b[a-z][a-z0-9]*_[a-z0-9_]+\b/u;

describe("M25-64 output facts", () => {
  const output = snapshotFixture(SMOKE_SHAPE).output;

  it("reads the decoded output profile for the header and every setting", () => {
    const width = output.width as number;
    const height = output.height as number;
    // The smoke fixture is 2,880 frames at 24/1: two minutes exactly.
    expect(output.durationFrames).toBe(2_880);
    expect(monitorHeaderFacts("en", output)).toBe(
      `${width} × ${height} · 24 fps`,
    );
    const rows = projectSettings("en", output);
    expect(rows.map((row) => row.key)).toEqual([
      "resolution",
      "frameRate",
      "duration",
      "audio",
      "export",
    ]);
    expect(Object.fromEntries(rows.map((row) => [row.key, row.value]))).toEqual(
      {
        resolution: `${width} × ${height}`,
        frameRate: "24 fps",
        duration: "00:02:00:00",
        audio: "AAC · 48 kHz · mono",
        export: "MP4 · H.264",
      },
    );
    expect(rows.find((row) => row.key === "duration")!.mono).toBe(true);
    expect(rows.filter((row) => row.mono)).toHaveLength(1);
  });

  it.each(LOCALES)("labels every setting plainly in %s", (locale) => {
    const rows = projectSettings(locale, output);
    expect(rows).toHaveLength(5);
    for (const row of rows) {
      expect(row.label.length, row.key).toBeGreaterThan(0);
      expect(`${row.label} ${row.value}`, row.key).not.toMatch(MACHINE_TOKEN);
      expect(row.value, row.key).not.toMatch(/\baac\b|\bh264\b|\bmp4\b/u);
    }
    expect(monitorHeaderFacts(locale, output)).toMatch(/^\d+ × \d+ · 24 fps$/u);
  });

  it("omits what the output does not state instead of inventing it", () => {
    expect(monitorHeaderFacts("en", undefined)).toBeNull();
    expect(monitorHeaderFacts("en", {})).toBeNull();
    expect(projectSettings("en", undefined)).toEqual([]);
    expect(projectSettings("en", {})).toEqual([]);
    // Width without height is no resolution; a zero denominator is no rate, and without a rate
    // a frame count is no timecode.
    expect(
      projectSettings("en", {
        width: 1920,
        frameRate: { num: 24, den: 0 },
        durationFrames: 48,
      }),
    ).toEqual([]);
    expect(
      monitorHeaderFacts("en", { width: 1920, height: 1080, frameRate: null }),
    ).toBe("1920 × 1080");
    expect(monitorHeaderFacts("en", { frameRate: { num: 25, den: 1 } })).toBe(
      "25 fps",
    );
    // Codec values the product does not name are left out, not shown raw.
    expect(
      projectSettings("en", {
        container: "mkv",
        videoCodec: "h264",
        audioCodec: "opus",
        channels: 2,
      }).map((row) => [row.key, row.value]),
    ).toEqual([
      ["audio", "stereo"],
      ["export", "H.264"],
    ]);
    expect(
      projectSettings("en", { sampleRate: 44_100, channels: 6 }).map(
        (row) => row.value,
      ),
    ).toEqual(["44.1 kHz · 6 channels"]);
    expect(projectSettings("en", { durationFrames: -1 })).toEqual([]);
  });

  it("keeps the sidebar summary's counting default and nothing else", () => {
    expect(nominalRate({ num: 24, den: 1 })).toBe(24);
    expect(nominalRate({ num: 30_000, den: 1_001 })).toBe(30);
    expect(nominalRate(undefined)).toBeNull();
    expect(nominalRate({ num: 0, den: 1 })).toBeNull();
    expect(nominalFps(undefined)).toBe(24);
    expect(nominalFps({ num: 25, den: 1 })).toBe(25);
  });
});
