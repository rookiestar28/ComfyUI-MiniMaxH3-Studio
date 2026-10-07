import { describe, expect, it } from "vitest";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import {
  buildPublicAssetManifest,
  validatePublicAssetManifest,
} from "../src/runtime/publicAssetManifest";

function overlaySnapshot(disposition: string, dualRole = false) {
  const wire = structuredClone(fixture.snapshot) as Record<string, unknown>;
  const assets = wire.assets as Array<Record<string, unknown>>;
  assets[1]!.embedded_audio = disposition;
  assets[1]!.source_sample_count =
    disposition === "present_bound" ? 48000 : null;
  if (dualRole) {
    const clips = wire.clips as Array<Record<string, unknown>>;
    clips[1]!.asset_id = "vid-primary";
  }
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return decodePublicCompositionSnapshot(wire);
}

describe("M25-18 measured source facts at the accepted browser manifest consumer", () => {
  it.each([
    "present_bound",
    "absent",
    "unavailable",
    "excluded_overlay_policy",
  ])(
    "preserves %s source metadata without deriving track policy",
    (disposition) => {
      const snapshot = overlaySnapshot(disposition);
      const before = structuredClone(snapshot);
      const manifest = buildPublicAssetManifest(snapshot);
      expect(manifest.assets[1]).toEqual({
        assetId: "vid-overlay",
        kind: "video",
        sourceTimeBase: snapshot.assets[1]!.sourceTimeBase,
        sourceFrameCount: snapshot.assets[1]!.sourceFrameCount,
        sourceSampleCount: disposition === "present_bound" ? 48000 : null,
        embeddedAudio: disposition,
        timestampPolicy: snapshot.assets[1]!.timestampPolicy,
        landmarks: snapshot.assets[1]!.landmarks,
      });
      expect(validatePublicAssetManifest(manifest, snapshot)).toBe(manifest);
      expect(snapshot).toEqual(before);
      expect(Object.isFrozen(manifest.assets[1]!.landmarks)).toBe(true);
    },
  );

  it("keeps one source entry for the same asset on primary and overlay tracks", () => {
    const snapshot = overlaySnapshot("absent", true);
    const manifest = buildPublicAssetManifest(snapshot);
    expect(snapshot.clips[0]!.assetId).toBe(snapshot.clips[1]!.assetId);
    const shared = manifest.assets.filter(
      (asset) => asset.assetId === "vid-primary",
    );
    expect(shared).toHaveLength(1);
    expect(shared[0]!.embeddedAudio).toBe("present_bound");
    expect(shared[0]!.sourceSampleCount).toBe(
      snapshot.assets[0]!.sourceSampleCount,
    );
    expect(validatePublicAssetManifest(manifest, snapshot)).toBe(manifest);
  });

  it("refuses a validly fingerprinted manifest for different source facts", () => {
    const audible = overlaySnapshot("present_bound");
    for (const disposition of [
      "absent",
      "unavailable",
      "excluded_overlay_policy",
    ]) {
      const different = overlaySnapshot(disposition);
      const manifest = buildPublicAssetManifest(different);
      expect(validatePublicAssetManifest(manifest, different)).toBe(manifest);
      expect(() => validatePublicAssetManifest(manifest, audible)).toThrow(
        /contract_mismatch/,
      );
    }
  });
});
