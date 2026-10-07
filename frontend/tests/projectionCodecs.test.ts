import { describe, expect, it } from "vitest";

import { decodeProductShellProjection } from "../src/contracts/projectionCodecs";

const fingerprint = `sha256:${"a".repeat(64)}`;
const valid = {
  schema: "h3.context.product.shell.v1",
  product_scope: "MANUAL_ONLY_SCOPED",
  qualification_plan_fingerprint: fingerprint,
  report_id: "report-1",
  report_revision: 1,
  report_fingerprint: fingerprint,
  prompt_fingerprint: fingerprint,
  correlation: { prompt_id: "prompt-1", execution_node_id: "17" },
  task_mode: "ref2va",
  profile: "full_reference",
  host: {
    node_api: "V1_ONLY",
    core_version: "0.32.0",
    core_revision: "b323a345bbbfb2f3a95b5b73b68eb7919a26515e",
    frontend_version: "1.48.7",
    frontend_revision: "6d6af63c00f132cd25dc29307fc56bd2c094fa22",
  },
  native_node_id: "MiniMaxH3ReferenceToVideo",
  prompt_export_ready: true,
  native_queue_ready: true,
  assisted_ready: false,
  readiness_reason: "manual_only_scoped",
  field_ids: ["h3.comfyui_h3_context_h3context_productshell.output.prompt"],
  bindings: [],
  limitations: ["visual.audio.live.profiles.unqualified"],
  assisted_authoring: {
    available: true,
    selected: false,
    ready: false,
    authorized_for_this_action: false,
    defaulted: false,
  },
};

describe("decodeProductShellProjection", () => {
  it("preserves the native hold without disabling prompt export or promoting readiness", () => {
    const held = {
      ...valid,
      native_queue_ready: false,
      readiness_reason: "native_input_unqualified",
    };
    expect(decodeProductShellProjection(held)).toEqual(held);
    for (const drift of [
      { ...held, native_queue_ready: true },
      { ...held, native_queue_ready: "false" },
      { ...held, readiness_reason: "manual_only_scoped" },
    ])
      expect(() => decodeProductShellProjection(drift)).toThrow();
  });

  it("accepts the closed manual-only projection", () => {
    expect(decodeProductShellProjection(valid)).toEqual(valid);
  });

  it("accepts well-formed host provenance without exact version pinning", () => {
    const changed = {
      ...valid,
      host: {
        ...valid.host,
        core_version: "0.33.0",
        core_revision: "1".repeat(40),
        frontend_version: "1.49.6",
        frontend_revision: "2".repeat(40),
      },
    };
    expect(decodeProductShellProjection(changed).host).toEqual(changed.host);
    expect(() =>
      decodeProductShellProjection({
        ...changed,
        host: { ...changed.host, core_version: "latest" },
      }),
    ).toThrow(/core_version.*invalid/i);
    expect(() =>
      decodeProductShellProjection({
        ...changed,
        host: { ...changed.host, core_version: `${"9".repeat(65)}.0` },
      }),
    ).toThrow(/core_version.*invalid/i);
  });

  it("rejects unknown members and assisted promotion", () => {
    expect(() =>
      decodeProductShellProjection({ ...valid, extra: true }),
    ).toThrow(/unknown/);
    expect(() =>
      decodeProductShellProjection({
        ...valid,
        product_scope: "ASSISTED_PROFILE_QUALIFIED",
        assisted_ready: true,
      }),
    ).toThrow(/manual-only/);
  });

  it("rejects sensitive text and unsafe binding paths", () => {
    expect(() =>
      decodeProductShellProjection({ ...valid, limitations: ["token=hidden"] }),
    ).toThrow(/sensitive/);
    expect(() =>
      decodeProductShellProjection({
        ...valid,
        bindings: [
          {
            asset_id: "image_1",
            kind: "image",
            presentation_label: "<Picture 1>",
            presentation_ordinal: 1,
            native_input: "ref_images",
            native_child_path:
              "MiniMaxH3ReferenceToVideo.ref_images.ref_image_1",
          },
        ],
      }),
    ).toThrow(/zero-based/);
  });

  it("accepts paired audio whose target is Video 2 and rejects an absent target", () => {
    const bindings = [
      {
        asset_id: "video_1",
        kind: "video",
        presentation_label: "<Video 1>",
        presentation_ordinal: 1,
        native_input: "ref_videos",
        native_child_path: "MiniMaxH3ReferenceToVideo.ref_videos.ref_video_0",
      },
      {
        asset_id: "audio_1",
        kind: "audio",
        presentation_label: "<Audio 1>",
        presentation_ordinal: 1,
        native_input: "ref_video_audios",
        native_child_path:
          "MiniMaxH3ReferenceToVideo.ref_video_audios.ref_video_audio_1",
      },
      {
        asset_id: "video_2",
        kind: "video",
        presentation_label: "<Video 2>",
        presentation_ordinal: 2,
        native_input: "ref_videos",
        native_child_path: "MiniMaxH3ReferenceToVideo.ref_videos.ref_video_1",
      },
    ] as const;
    expect(decodeProductShellProjection({ ...valid, bindings })).toMatchObject({
      bindings,
    });
    expect(() =>
      decodeProductShellProjection({
        ...valid,
        bindings: bindings.slice(0, 2),
      }),
    ).toThrow(/paired video target/);
  });
});
