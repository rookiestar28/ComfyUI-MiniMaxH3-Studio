import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  referenceCapacity,
  referenceLabels,
} from "../src/host/referenceSelection";
import { AppModeError, validateInputs } from "../src/host/appModeContract";

type Case = {
  name: string;
  images: number;
  videos: number;
  audios: number;
  soundtrack: "included" | "excluded" | "unavailable";
};
const fixture = (kind: string) =>
  JSON.parse(
    readFileSync(
      join(
        dirname(fileURLToPath(import.meta.url)),
        "..",
        "..",
        "tests",
        "fixtures",
        `ref2va_reference_${kind}_v1.json`,
      ),
      "utf8",
    ),
  );
const selection = (row: Case) => ({
  images: row.images,
  videos: row.videos,
  audios: row.audios,
  soundtrack: row.soundtrack,
});

describe("two-ended reference contracts", () => {
  for (const row of fixture("capacity").cases) {
    it(`matches native capacity and sidebar policy: ${row.name}`, () => {
      expect(referenceCapacity(selection(row))).toMatchObject({
        files: row.files,
        audio: row.audio_total,
        capacityAllowed: row.capacity_allowed,
        allowed: row.allowed,
        reason: row.reason,
      });
      const inputs = {
        task_mode: "ref2va" as const,
        user_intent: "Preserve ordered references.",
        duration_milliseconds: 5167,
        frame_count: 124,
        reference_image_sources: Array.from({ length: row.images }, (_, i) =>
          String(10 + i),
        ),
        reference_video_sources: Array.from({ length: row.videos }, (_, i) =>
          String(30 + i),
        ),
        reference_audio_sources: Array.from({ length: row.audios }, (_, i) =>
          String(40 + i),
        ),
        ...(row.videos > 0
          ? { reference_video_soundtrack: row.soundtrack }
          : {}),
      };
      if (row.allowed) expect(() => validateInputs(inputs)).not.toThrow();
      else {
        expect(() => validateInputs(inputs)).toThrow(AppModeError);
        try {
          validateInputs(inputs);
        } catch (error) {
          expect((error as AppModeError).code).toBe(
            row.reason === "visual"
              ? "invalid_request"
              : "reference_capacity_exceeded",
          );
        }
      }
    });
  }
  for (const row of fixture("labels").cases) {
    it(`matches exact Registry label order: ${row.name}`, () => {
      expect(referenceLabels(selection(row))).toEqual(row.labels);
    });
  }
  it("accounts for soundtrack when adding or toggling", () => {
    expect(
      referenceCapacity({
        images: 9,
        videos: 0,
        audios: 3,
        soundtrack: "included",
      }).remaining,
    ).toEqual({ images: 0, videos: 0, audios: 0 });
    expect(
      referenceCapacity({
        images: 9,
        videos: 3,
        audios: 0,
        soundtrack: "excluded",
      }).canIncludeSoundtrack,
    ).toBe(false);
  });
});
