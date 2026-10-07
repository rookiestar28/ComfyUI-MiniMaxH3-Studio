// M25-20 corrective: the browser stage could not present one corpus composition at all. The monitor
// read "Monitor unavailable: invalid contract.", with no console error, no page error and no failed
// request, so it was not a media-load problem. This pins the failure where it actually happens --
// in the pure resolver and its own decoder -- instead of through a browser, and it names the frame.
//
// The composition is `embedded_audio.primary_without_audio`: `clip-main` is replaced with an asset
// declared `excluded_overlay_policy`, so the first primary clip carries no bound audio while the
// second one does. Nothing about that is malformed, and the backend accepts it.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
} from "../src/contracts/compositionCodec";
import { resolveCompositionScene } from "../src/runtime/sceneResolver";

const WIRE_PATH = join(
  __dirname,
  "fixtures",
  "m25_20_primary_without_audio_snapshot.json",
);

const snapshotWire = JSON.parse(readFileSync(WIRE_PATH, "utf8")) as unknown;

describe("scene resolution for a primary clip that carries no bound audio", () => {
  it("decodes the composition the backend accepted", () => {
    expect(() => decodePublicCompositionSnapshot(snapshotWire)).not.toThrow();
  });

  it("resolves and re-decodes every output frame", () => {
    const snapshot = decodePublicCompositionSnapshot(snapshotWire);
    const durationFrames = Number(
      (snapshot.output as { durationFrames: unknown }).durationFrames,
    );
    expect(durationFrames).toBeGreaterThan(0);
    const failures: Array<{ frame: number; message: string }> = [];
    for (let frame = 0; frame < durationFrames; frame += 1) {
      try {
        decodeResolvedScene(resolveCompositionScene(snapshot, frame));
      } catch (error) {
        failures.push({
          frame,
          message: error instanceof Error ? error.message : String(error),
        });
      }
    }
    expect(failures).toEqual([]);
  });
});
