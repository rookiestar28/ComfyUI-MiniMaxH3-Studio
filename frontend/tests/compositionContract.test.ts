import { describe, expect, it } from "vitest";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  BLOCKER_CODES,
  NLE_OPERATION_IDS,
  PUBLIC_SNAPSHOT_SCHEMA,
  RESOLVED_SCENE_SCHEMA,
  decodeCompositionJson,
  decodePublicCompositionSnapshot,
  decodeResolvedScene,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";

function snapshotWire(): Record<string, unknown> {
  return structuredClone(fixture.snapshot) as Record<string, unknown>;
}

function resolvedSceneWire(): Record<string, unknown> {
  const clip = structuredClone(fixture.snapshot.clips[0]);
  return {
    schema: RESOLVED_SCENE_SCHEMA,
    profile_id: "h3.native_media_canvas_backend.v1",
    public_fingerprint: fixture.snapshot.public_fingerprint,
    frame: fixture.expectations.frame,
    layers: [
      {
        clip_id: clip.clip_id,
        asset_id: clip.asset_id,
        track_id: clip.track_id,
        source_frame: fixture.expectations.frame,
        source_pts: fixture.expectations.source_pts,
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
    ],
    audio_span: {
      asset_id: "vid-primary",
      clip_id: "clip-main",
      output_start_sample: fixture.expectations.output_start_sample,
      output_end_sample: fixture.expectations.output_end_sample,
      source_start_sample: fixture.expectations.source_start_sample,
      source_end_sample: fixture.expectations.source_end_sample,
    },
    blockers: [],
  };
}

describe("M25-10 public composition contract", () => {
  it.each(["present_bound", "absent", "unavailable"])(
    "admits measured %s overlay facts without rewriting their identity",
    (disposition) => {
      const wire = snapshotWire();
      const assets = wire.assets as Array<Record<string, unknown>>;
      assets[1]!.embedded_audio = disposition;
      assets[1]!.source_sample_count =
        disposition === "present_bound" ? 48000 : null;
      wire.public_fingerprint = publicCompositionFingerprint(wire);
      const before = structuredClone(wire);
      const snapshot = decodePublicCompositionSnapshot(wire);
      expect(snapshot.assets[1]?.embeddedAudio).toBe(disposition);
      expect(snapshot.assets[1]?.sourceSampleCount).toBe(
        assets[1]!.source_sample_count,
      );
      expect(snapshot.publicFingerprint).toBe(wire.public_fingerprint);
      const expected: Record<string, string> = {
        present_bound:
          "sha256:779a7a086b4f0d75dad978060de5d839bd5ad4aa7a95596e40530163bd161793",
        absent:
          "sha256:50f5a4630528f85d2d76fbcbcfc27bdb4faa461eb21e2f07245980f419e9e041",
        unavailable:
          "sha256:f5b5151347929e2d128ab696b158270efce18a9de9f5ca34bd4406cc7227b227",
      };
      expect(snapshot.publicFingerprint).toBe(expected[disposition]);
      expect(wire).toEqual(before);
    },
  );

  it("admits one unchanged source identity in primary and overlay clips", () => {
    const wire = snapshotWire();
    const clips = wire.clips as Array<Record<string, unknown>>;
    clips[1]!.asset_id = "vid-primary";
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const snapshot = decodePublicCompositionSnapshot(wire);
    expect(snapshot.clips[0]?.assetId).toBe(snapshot.clips[1]?.assetId);
    expect(snapshot.assets[0]?.embeddedAudio).toBe("present_bound");
  });

  it("preserves historical policy-labelled bytes and their original hash", () => {
    const wire = snapshotWire();
    const before = structuredClone(wire);
    const snapshot = decodePublicCompositionSnapshot(wire);
    expect(snapshot.assets[1]?.embeddedAudio).toBe("excluded_overlay_policy");
    expect(snapshot.publicFingerprint).toBe(
      fixture.snapshot.public_fingerprint,
    );
    expect(wire).toEqual(before);
  });

  it("decodes and freezes the shared closed fixture", () => {
    const snapshot = decodePublicCompositionSnapshot(snapshotWire());
    expect(snapshot.schema).toBe(PUBLIC_SNAPSHOT_SCHEMA);
    expect(snapshot.publicFingerprint).toBe(
      fixture.snapshot.public_fingerprint,
    );
    expect(snapshot.assets).toHaveLength(4);
    expect(snapshot.clips).toHaveLength(4);
    expect(Object.isFrozen(snapshot)).toBe(true);
    expect(Object.isFrozen(snapshot.assets)).toBe(true);
    expect(Object.isFrozen(snapshot.clips[0]?.transform)).toBe(true);
  });

  it("binds every selected capability and accepted observation fact", () => {
    const capability = snapshotWire().capability as Record<string, unknown>;
    expect(capability.input_containers).toEqual(["mp4"]);
    expect(capability.input_video_codecs).toEqual(["h264"]);
    expect(capability.input_audio_codecs).toEqual(["aac"]);
    expect(capability.input_pixel_formats).toEqual(["yuv420p"]);
    expect(capability.frame_event_fallbacks).toEqual(["seeked", "timeupdate"]);
    expect(capability.observation_corpus_ids).toEqual([
      "cfr",
      "invalid",
      "lane",
      "mse",
      "truncated",
      "vfr",
    ]);
    expect(capability.probe_root_keys).toEqual([
      "format",
      "programs",
      "stream_groups",
      "streams",
    ]);
    expect(capability.probe_audio_stream_keys).toEqual([
      "avg_frame_rate",
      "channel_layout",
      "channels",
      "codec_name",
      "codec_type",
      "sample_rate",
    ]);
    expect(capability.audio_stream_rate_sentinel).toBe("0/0");

    for (const [key, value] of Object.entries(capability)) {
      const drifted = snapshotWire();
      const target = drifted.capability as Record<string, unknown>;
      target[key] =
        typeof value === "boolean"
          ? !value
          : typeof value === "number"
            ? value + 1
            : typeof value === "string"
              ? `${value}.drift`
              : [...(value as unknown[]), "drift"];
      drifted.public_fingerprint = publicCompositionFingerprint(drifted);
      expect(() => decodePublicCompositionSnapshot(drifted), key).toThrow(
        /unsupported_profile/,
      );
    }
  });

  it("reproduces the Python canonical fingerprint exactly", () => {
    expect(publicCompositionFingerprint(snapshotWire())).toBe(
      fixture.snapshot.public_fingerprint,
    );
  });

  it("admits the exact M25-09 CFR source clock without widening output rationals", () => {
    const wire = snapshotWire();
    const asset = (wire.assets as Array<Record<string, unknown>>)[0]!;
    asset.source_time_base = { num: 1, den: 12_288 };
    for (const landmark of asset.landmarks as Array<Record<string, number>>) {
      landmark.pts = landmark.frame_index * 512;
      landmark.dts = landmark.frame_index * 512;
      landmark.duration_ticks = 512;
    }
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    expect(
      decodePublicCompositionSnapshot(wire).assets[0]?.sourceTimeBase,
    ).toEqual({ num: 1, den: 12_288 });

    const outputDrift = snapshotWire();
    (outputDrift.output as Record<string, unknown>).time_base = {
      num: 1,
      den: 12_288,
    };
    outputDrift.public_fingerprint = publicCompositionFingerprint(outputDrift);
    expect(() => decodePublicCompositionSnapshot(outputDrift)).toThrow(
      /invalid_contract/,
    );

    const unsafeSource = snapshotWire();
    const unsafeAsset = (
      unsafeSource.assets as Array<Record<string, unknown>>
    )[0]!;
    unsafeAsset.source_time_base = {
      num: 1,
      den: Number.MAX_SAFE_INTEGER + 1,
    };
    expect(() => decodePublicCompositionSnapshot(unsafeSource)).toThrow(
      /invalid_contract/,
    );
  });

  it("rejects unknown, private, duplicate and stale fields", () => {
    expect(() =>
      decodePublicCompositionSnapshot({ ...snapshotWire(), surprise: true }),
    ).toThrow(/invalid_contract/);
    expect(() =>
      decodePublicCompositionSnapshot({
        ...snapshotWire(),
        runtime_identity: "private",
      }),
    ).toThrow(/private_field/);
    expect(() =>
      decodePublicCompositionSnapshot({
        ...snapshotWire(),
        public_fingerprint: `sha256:${"f".repeat(64)}`,
      }),
    ).toThrow(/stale_snapshot/);
    const text = JSON.stringify(snapshotWire()).replace(
      '"project_id":',
      '"project_id":"duplicate","project_id":',
    );
    expect(() => decodeCompositionJson(text)).toThrow(/invalid_contract/);
  });

  it("exposes finite operation and blocker vocabularies", () => {
    // 33 accepted operations and `set_clip_audio`, a video clip's own gain, mute and fades.
    expect(NLE_OPERATION_IDS).toHaveLength(34);
    expect(new Set(NLE_OPERATION_IDS).size).toBe(34);
    expect(BLOCKER_CODES).toContain("audio_editing_deferred");
    expect(BLOCKER_CODES).toContain("private_field");
  });

  it("rejects negative and non-monotonic timing", () => {
    const negative = snapshotWire();
    const assets = negative.assets as Array<Record<string, unknown>>;
    const landmarks = assets[0]?.landmarks as Array<Record<string, unknown>>;
    if (landmarks[0]) landmarks[0].pts = -1;
    expect(() => decodePublicCompositionSnapshot(negative)).toThrow(
      /negative_timestamp/,
    );

    const nonmonotonic = snapshotWire();
    const rows = (nonmonotonic.assets as Array<Record<string, unknown>>)[0]
      ?.landmarks as Array<Record<string, unknown>> | undefined;
    if (rows?.[1]) rows[1].pts = 0;
    expect(() => decodePublicCompositionSnapshot(nonmonotonic)).toThrow(
      /invalid_timing/,
    );

    const outsideSource = snapshotWire();
    const outsideAssets = outsideSource.assets as Array<
      Record<string, unknown>
    >;
    const outsideRows = outsideAssets[0]?.landmarks as
      Array<Record<string, unknown>> | undefined;
    if (outsideRows?.[2]) outsideRows[2].frame_index = 100;
    outsideSource.public_fingerprint =
      publicCompositionFingerprint(outsideSource);
    expect(() => decodePublicCompositionSnapshot(outsideSource)).toThrow(
      /invalid_timing/,
    );
  });

  it("rejects non-NFC text instead of accepting a cross-language rewrite", () => {
    const wire = snapshotWire();
    const clips = wire.clips as Array<Record<string, unknown>>;
    const text = clips[3]?.text as Record<string, unknown>;
    text.content = "Cafe\u0301";
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    expect(() => decodePublicCompositionSnapshot(wire)).toThrow(
      /invalid_contract/,
    );
  });

  it("requires a cross dissolve to have a lower-layer participant", () => {
    const wire = snapshotWire();
    const clips = wire.clips as Array<Record<string, unknown>>;
    if (clips[0]) clips[0].duration_frames = 12;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    expect(() => decodePublicCompositionSnapshot(wire)).toThrow(
      /invalid_contract/,
    );
  });

  it("rejects a third concurrently active video clip", () => {
    const wire = snapshotWire();
    const clips = wire.clips as Array<Record<string, unknown>>;
    const incoming = structuredClone(clips[0]) as Record<string, unknown>;
    incoming.clip_id = "clip-incoming";
    incoming.start_frame = 12;
    incoming.duration_frames = 12;
    clips.push(incoming);
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    expect(() => decodePublicCompositionSnapshot(wire)).toThrow(
      /resource_limit/,
    );
  });

  it("strictly decodes the pure resolver hand-off", () => {
    const scene = decodeResolvedScene(resolvedSceneWire());
    expect(scene.frame).toBe(12);
    expect(scene.layers).toHaveLength(1);
    expect(Object.isFrozen(scene)).toBe(true);
    expect(() =>
      decodeResolvedScene({
        schema: RESOLVED_SCENE_SCHEMA,
        profile_id: "h3.native_media_canvas_backend.v1",
        public_fingerprint: fixture.snapshot.public_fingerprint,
        frame: 12,
        layers: [],
        audio_span: null,
        blockers: [],
        source_url: "private",
      }),
    ).toThrow(/private_field/);
  });

  it("rejects semantically impossible resolved operations, crop and effects", () => {
    const mutations: Array<(wire: Record<string, unknown>) => void> = [
      (wire) => {
        const operations = ((wire.layers as Array<Record<string, unknown>>)[0]
          ?.operation_ids ?? []) as string[];
        [operations[0], operations[1]] = [operations[1]!, operations[0]!];
      },
      (wire) => {
        const operations = ((wire.layers as Array<Record<string, unknown>>)[0]
          ?.operation_ids ?? []) as string[];
        operations.pop();
      },
      (wire) => {
        const layer = (wire.layers as Array<Record<string, unknown>>)[0]!;
        (layer.operation_ids as string[]).push("CrossDissolveV1");
      },
      (wire) => {
        const layer = (wire.layers as Array<Record<string, unknown>>)[0]!;
        layer.transition_elapsed_frames = 0;
      },
      (wire) => {
        const layer = (wire.layers as Array<Record<string, unknown>>)[0]!;
        (layer.operation_ids as string[]).push("DrawTextV1");
      },
      (wire) => {
        const layer = (wire.layers as Array<Record<string, unknown>>)[0]!;
        (layer.crop as Record<string, number>).left_bp = 5_000;
        (layer.crop as Record<string, number>).right_bp = 5_000;
      },
      (wire) => {
        const layer = (wire.layers as Array<Record<string, unknown>>)[0]!;
        (layer.effect as Record<string, number>).brightness_permille = 1;
      },
    ];
    for (const mutate of mutations) {
      const wire = resolvedSceneWire();
      mutate(wire);
      expect(() => decodeResolvedScene(wire)).toThrow(/invalid_contract/);
    }
  });
});
