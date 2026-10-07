import { describe, expect, it } from "vitest";

import {
  AUTHORING_AUDIO_PEAKS_MAX_BYTES,
  decodeAuthoringAudioPeaks,
  disposeAuthoringAudioPeaks,
} from "../src/contracts/authoringAudioPeaks";

const envelope = (
  sampleCount: number,
  pairs: readonly (readonly [number, number])[],
) => {
  const bytes = new Uint8Array(20 + pairs.length * 2);
  bytes.set([0x48, 0x33, 0x41, 0x50]);
  const view = new DataView(bytes.buffer);
  view.setUint16(4, 1, true);
  view.setUint16(6, 100, true);
  view.setUint32(8, 8_000, true);
  view.setUint32(12, sampleCount, true);
  view.setUint32(16, pairs.length, true);
  pairs.forEach(([low, high], index) => {
    view.setInt8(20 + index * 2, low);
    view.setInt8(21 + index * 2, high);
  });
  return bytes;
};

describe("authoring audio peaks envelope", () => {
  it("decodes the closed little-endian envelope without creating a media surface", () => {
    const decoded = decodeAuthoringAudioPeaks(
      envelope(81, [
        [-128, 127],
        [0, 0],
      ]),
    );

    expect(decoded).toMatchObject({
      version: 1,
      pairRate: 100,
      sampleRate: 8_000,
      sampleCount: 81,
      pairCount: 2,
    });
    expect([...decoded.pairs]).toEqual([-128, 127, 0, 0]);
    expect(AUTHORING_AUDIO_PEAKS_MAX_BYTES).toBe(32_788);
    disposeAuthoringAudioPeaks(decoded);
    expect([...decoded.pairs]).toEqual([0, 0, 0, 0]);
  });

  it.each([
    envelope(80, [[1, -1]]),
    envelope(81, [[0, 0]]),
    envelope(80, [[0, 0]]).subarray(0, 21),
    new Uint8Array(0),
  ])("rejects malformed, inverted, mismatched or truncated data", (bytes) => {
    expect(() => decodeAuthoringAudioPeaks(bytes)).toThrow(
      "invalid audio peaks envelope",
    );
  });
});
