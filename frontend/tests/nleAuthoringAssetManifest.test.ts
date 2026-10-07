import { describe, expect, it } from "vitest";

import {
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
  type NleAuthoringStateV2,
} from "../src/contracts/authoringWorkbenchCodec";
import type { PublicCompositionAsset } from "../src/contracts/compositionCodec";
import {
  buildNleAuthoringAssetManifest,
  validateNleAuthoringAssetManifest,
} from "../src/runtime/nleAuthoringAssetManifest";

const authoring: NleAuthoringStateV2 = {
  schema: NLE_AUTHORING_SCHEMA,
  profileId: NLE_AUTHORING_PROFILE_ID,
  operationProfileId: NLE_OPERATION_PROFILE_ID_V2,
  projectId: "project.1",
  workspaceHandle: "workspace.1",
  workspaceRevision: 1,
  workspaceFingerprint: `sha256:${"1".repeat(64)}`,
  timelineRevision: 1,
  timelineFingerprint: `sha256:${"2".repeat(64)}`,
  authoringFingerprint: `sha256:${"3".repeat(64)}`,
  editCapacityFrames: 3_600,
  contentEndExclusive: 0,
  assets: [],
  tracks: [
    {
      trackId: "track.primary",
      kind: "primary_video",
      order: 0,
      enabled: true,
      locked: false,
    },
  ],
  clips: [],
  audioExtension: Object.freeze({}),
  blockers: [],
};

describe("NLE authoring asset manifest", () => {
  it("binds the full catalog and 512 timing rows even without a render snapshot", () => {
    const video: PublicCompositionAsset = {
      assetId: "asset.video",
      kind: "video",
      sourceTimeBase: { num: 1, den: 24 },
      sourceFrameCount: 512,
      sourceSampleCount: null,
      embeddedAudio: "absent",
      timestampPolicy: "nonnegative_monotonic_v1",
      landmarks: Array.from({ length: 512 }, (_, frameIndex) => ({
        frameIndex,
        pts: frameIndex * 512,
        dts: frameIndex * 512,
        durationTicks: 512,
      })),
    };
    const empty = { ...authoring, assets: [video] };
    const manifest = buildNleAuthoringAssetManifest(empty);

    expect(manifest.schema).toBe("h3.context.nle_asset_manifest.v1");
    expect(manifest.authoringFingerprint).toBe(authoring.authoringFingerprint);
    expect(manifest.assets[0]?.landmarks).toHaveLength(512);
    expect(manifest.manifestFingerprint).toMatch(/^sha256:[0-9a-f]{64}$/u);
    expect(validateNleAuthoringAssetManifest(manifest, empty)).toBe(manifest);
    expect(
      buildNleAuthoringAssetManifest({
        ...empty,
        authoringFingerprint: `sha256:${"4".repeat(64)}`,
      }).manifestFingerprint,
    ).not.toBe(manifest.manifestFingerprint);
  });

  it("rejects a manifest retained from another authoring identity", () => {
    const manifest = buildNleAuthoringAssetManifest(authoring);
    expect(() =>
      validateNleAuthoringAssetManifest(manifest, {
        ...authoring,
        timelineRevision: authoring.timelineRevision + 1,
      }),
    ).toThrow(/does not bind authoring state/u);
  });
});
