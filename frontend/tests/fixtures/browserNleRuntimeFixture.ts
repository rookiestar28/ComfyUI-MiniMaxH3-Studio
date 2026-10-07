import fixture from "../../../tests/fixtures/m25_10_composition_contract_v1.json";
import vfrTiming from "../../../tests/fixtures/m25_12_runtime/timing.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
  RESOLVED_SCENE_SCHEMA,
  type PublicCompositionSnapshot,
} from "../../src/contracts/compositionCodec";
import {
  ACCEPTED_CORPUS_FINGERPRINTS,
  RUNTIME_PROFILE,
  RUNTIME_PROFILE_FINGERPRINT,
  RUNTIME_QUALIFICATION_SCHEMA,
  runtimeQualificationFingerprint,
  type RuntimeQualification,
  type RuntimeQualificationAuthority,
  type RuntimeQualificationPayload,
} from "../../src/runtime/mediaCapabilities";

export function runtimeFixtureSnapshot(
  revision = 11,
  encodedMedia?: string,
): PublicCompositionSnapshot {
  const wire = structuredClone(fixture.snapshot);
  wire.timeline_revision = revision;
  if (encodedMedia) {
    wire.assets[0]!.source_frame_count = 48;
    wire.assets[0]!.source_sample_count = 96000;
    wire.assets[1]!.source_frame_count = 48;
    if (encodedMedia === "vfr") {
      wire.assets[0]!.source_frame_count = vfrTiming.source_frame_count;
      wire.assets[0]!.source_sample_count = null;
      wire.assets[0]!.embedded_audio = "absent";
      // The public contract accepts sparse landmarks. Omit genuinely unavailable DTS rows;
      // never replace null DTS with PTS or pretend the filtered index is the source frame index.
      wire.assets[0]!.landmarks = vfrTiming.frames
        .filter((row) => row.dts !== null)
        .map((row) => ({ ...row, dts: row.dts! }));
      wire.output.duration_frames = vfrTiming.source_frame_count;
      for (const clip of wire.clips)
        clip.duration_frames = Math.min(
          clip.duration_frames,
          wire.output.duration_frames - clip.start_frame,
        );
    }
  }
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return decodePublicCompositionSnapshot(wire);
}

// This is test admission only. Real codec/resource qualification comes from measured browser
// journeys and their externally retained receipt, never from these fixture values.
export function runtimeContractAdmission(
  overrides: Partial<RuntimeQualificationPayload> = {},
): Readonly<{
  qualification: RuntimeQualification;
  authority: RuntimeQualificationAuthority;
}> {
  const payload: RuntimeQualificationPayload = {
    schema: RUNTIME_QUALIFICATION_SCHEMA,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    result: "pass",
    browser: RUNTIME_PROFILE.browser,
    corpusFingerprints: [...ACCEPTED_CORPUS_FINGERPRINTS],
    maximumActiveVideoOwners: 2,
    maximumWarmVideoOwners: 0,
    maximumCanvasOwners: 0,
    maximumPendingRvfc: 2,
    maximumPendingOperations: 2,
    maximumCancelMs: 0,
    maximumTeardownMs: 0,
    maximumJsHeapDeltaBytes: 0,
    ownedResourcesAfterTeardown: 0,
    ...overrides,
  };
  const receiptFingerprint = runtimeQualificationFingerprint(payload);
  return {
    qualification: { ...payload, receiptFingerprint },
    authority: {
      expectedProfileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
      expectedQualificationFingerprint: receiptFingerprint,
    },
  };
}

export function runtimeFixtureScene(
  frame: number,
  snapshot: PublicCompositionSnapshot,
  dual = false,
) {
  const clip = fixture.snapshot.clips[0];
  return {
    schema: RESOLVED_SCENE_SCHEMA,
    profile_id: snapshot.profileId,
    public_fingerprint: snapshot.publicFingerprint,
    frame,
    layers: [
      {
        clip_id: clip.clip_id,
        asset_id: clip.asset_id,
        track_id: clip.track_id,
        source_frame: frame,
        source_pts:
          snapshot.assets[0]!.landmarks.find(
            (landmark) => landmark.frameIndex === frame,
          )?.pts ?? frame * 512,
        transition_elapsed_frames: null,
        operation_ids: [
          "SelectSourceRangeV1",
          "CropV1",
          "Transform2DV1",
          "OpacityV1",
          "BlendV1",
        ],
        transform: clip.transform,
        crop: clip.crop,
        opacity_bp: clip.opacity_bp,
        blend: clip.blend,
        text: clip.text,
        effect: clip.effect,
      },
      ...(dual && frame >= 12 && frame < 24
        ? [
            {
              clip_id: fixture.snapshot.clips[1].clip_id,
              asset_id: fixture.snapshot.clips[1].asset_id,
              track_id: fixture.snapshot.clips[1].track_id,
              source_frame: frame - 12,
              source_pts: (frame - 12) * 512,
              transition_elapsed_frames: frame - 12,
              operation_ids: [
                "SelectSourceRangeV1",
                "CropV1",
                "Transform2DV1",
                "ColorAdjustV1",
                "OpacityV1",
                "BlendV1",
                "CrossDissolveV1",
              ],
              transform: fixture.snapshot.clips[1].transform,
              crop: fixture.snapshot.clips[1].crop,
              opacity_bp: fixture.snapshot.clips[1].opacity_bp,
              blend: fixture.snapshot.clips[1].blend,
              text: fixture.snapshot.clips[1].text,
              effect: fixture.snapshot.clips[1].effect,
            },
          ]
        : []),
    ],
    audio_span: null,
    blockers: [],
  };
}
