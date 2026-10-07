import { describe, expect, it } from "vitest";

import { decodeAuthoringAudioPeaks } from "../src/contracts/authoringAudioPeaks";
import {
  MAX_NLE_AUDIO_PEAKS_BYTES,
  MAX_NLE_AUDIO_PEAKS_ENVELOPES,
  createNleAudioPeaksCache,
} from "../src/host/nleAudioPeaksCache";

const peaks = (value: number) => {
  const bytes = new Uint8Array(22);
  bytes.set([0x48, 0x33, 0x41, 0x50]);
  const view = new DataView(bytes.buffer);
  view.setUint16(4, 1, true);
  view.setUint16(6, 100, true);
  view.setUint32(8, 8_000, true);
  view.setUint32(12, 80, true);
  view.setUint32(16, 1, true);
  view.setInt8(20, -value);
  view.setInt8(21, value);
  return decodeAuthoringAudioPeaks(bytes);
};

const key = (asset: string) => ({
  workspaceHandle: "workspace-1",
  assetFingerprint: `sha256:${asset.padStart(64, "0")}`,
  derivativeProfileId: "h3.authoring.media_derivatives.v6",
  derivativeKind: "audio_peaks" as const,
});

describe("NLE audio peaks cache", () => {
  it("uses bounded LRU entries and clears evicted amplitudes", () => {
    const cache = createNleAudioPeaksCache({ entries: 2, bytes: 44 });
    const first = peaks(1);
    const second = peaks(2);
    cache.set(key("1"), first);
    cache.set(key("2"), second);
    expect(cache.get(key("1"))).toBe(first);
    cache.set(key("3"), peaks(3));

    expect(cache.get(key("2"))).toBeUndefined();
    expect([...second.pairs]).toEqual([0, 0]);
    expect(cache.size()).toBe(2);
    cache.close();
    expect([...first.pairs]).toEqual([0, 0]);
  });

  it("publishes the fixed privacy and resource ceilings", () => {
    expect(MAX_NLE_AUDIO_PEAKS_ENVELOPES).toBe(32);
    expect(MAX_NLE_AUDIO_PEAKS_BYTES).toBe(2 * 1024 * 1024);
  });
});
