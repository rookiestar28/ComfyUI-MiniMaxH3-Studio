// A content-free authoring catalog for the preparation tests: every kind of asset the lease
// boundary tells apart, each named for the fact that decides whether it is prepared.

import {
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
  type NleAuthoringStateV2,
} from "../../src/contracts/authoringWorkbenchCodec";
import type { PublicCompositionAsset } from "../../src/contracts/compositionCodec";

const fingerprint = (digit: string) => `sha256:${digit.repeat(64)}`;

const landmarks = (count: number) =>
  Array.from({ length: count }, (_, frameIndex) => ({
    frameIndex,
    pts: frameIndex * 512,
    dts: frameIndex * 512,
    durationTicks: 512,
  }));

const video = (
  assetId: string,
  frames: number,
  rows: number,
  audio: Pick<PublicCompositionAsset, "embeddedAudio" | "sourceSampleCount">,
): PublicCompositionAsset => ({
  assetId,
  kind: "video",
  sourceTimeBase: { num: 1, den: 12_288 },
  sourceFrameCount: frames,
  timestampPolicy: "nonnegative_monotonic_v1",
  landmarks: landmarks(rows),
  ...audio,
});

const untimed = (
  assetId: string,
  kind: "image" | "font",
): PublicCompositionAsset => ({
  assetId,
  kind,
  sourceTimeBase: null,
  sourceFrameCount: null,
  sourceSampleCount: null,
  embeddedAudio: "absent",
  timestampPolicy: "not_applicable",
  landmarks: [],
});

export const PREPARATION_ASSETS: readonly PublicCompositionAsset[] = [
  untimed("img-still", "image"),
  video("vid-audio", 48, 48, {
    embeddedAudio: "present_bound",
    sourceSampleCount: 144_000,
  }),
  untimed("font-face", "font"),
  video("vid-silent", 12, 12, {
    embeddedAudio: "absent",
    sourceSampleCount: null,
  }),
  // One timing row per frame is what makes a source's whole span leasable; this one has two.
  video("vid-sparse", 48, 2, {
    embeddedAudio: "present_bound",
    sourceSampleCount: 144_000,
  }),
  // Bound audio without a sample count cannot have its preview checked for coverage.
  video("vid-uncounted", 24, 24, {
    embeddedAudio: "present_bound",
    sourceSampleCount: null,
  }),
];

/**
 * Two states the composition decoder never makes: it gives an image no timing, and it gives a
 * sample count only to audio that is bound. The preparation hook and the lease client's member
 * are handed a catalog and not a decoder's word, so their tests hand them these as well.
 */
export const PREPARATION_ASSETS_NO_DECODER_MAKES: readonly PublicCompositionAsset[] =
  [
    // Timed like a video in every field: only its kind keeps it from being prepared.
    {
      ...video("img-timed", 12, 12, {
        embeddedAudio: "absent",
        sourceSampleCount: null,
      }),
      kind: "image",
    },
    // A sample count beside audio that is not bound: only the audio's state refuses its preview.
    video("vid-unbound", 24, 24, {
      embeddedAudio: "unavailable",
      sourceSampleCount: 72_000,
    }),
  ];

/** An accepted state over the catalog; `revision` stands for each later accepted edit. */
export function preparationAuthoring(
  revision = 1,
  assets: readonly PublicCompositionAsset[] = PREPARATION_ASSETS,
): NleAuthoringStateV2 {
  return {
    schema: NLE_AUTHORING_SCHEMA,
    profileId: NLE_AUTHORING_PROFILE_ID,
    operationProfileId: NLE_OPERATION_PROFILE_ID_V2,
    projectId: "project-preparation",
    workspaceHandle: "workspace-preparation",
    workspaceRevision: 7,
    workspaceFingerprint: fingerprint("6"),
    timelineRevision: 10 + revision,
    timelineFingerprint: fingerprint("7"),
    authoringFingerprint: fingerprint(String(revision % 10)),
    editCapacityFrames: 3_600,
    contentEndExclusive: 0,
    assets,
    tracks: [],
    clips: [],
    audioExtension: Object.freeze({}),
    blockers: [],
  };
}
