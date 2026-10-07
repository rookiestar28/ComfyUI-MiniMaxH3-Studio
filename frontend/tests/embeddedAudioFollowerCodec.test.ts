import { describe, expect, it } from "vitest";

import {
  EMBEDDED_AUDIO_FOLLOWER_SCHEMA,
  decodeEmbeddedAudioFollowerStatus,
} from "../src/contracts/embeddedAudioFollowerCodec";

function status() {
  return {
    schema: EMBEDDED_AUDIO_FOLLOWER_SCHEMA,
    policy_id: "primary_embedded_follow_video_v1",
    transport_epoch: 4,
    scene_fingerprint: `sha256:${"1".repeat(64)}`,
    owner_clip_id: "clip-primary",
    state: "following",
    reason: "none",
    preview_capability: "available",
    final_render_capability: "not_evaluated",
  };
}

describe("embedded VIDEO audio status boundary", () => {
  it("decodes a bounded status without promoting final-render qualification", () => {
    const value = decodeEmbeddedAudioFollowerStatus(status());
    expect(value).toEqual(status());
    expect(Object.isFrozen(value)).toBe(true);
    expect(value.final_render_capability).toBe("not_evaluated");
  });

  it.each([
    { schema: "future" },
    { policy_id: "independent_audio" },
    { transport_epoch: -1 },
    { transport_epoch: Number.MAX_SAFE_INTEGER + 1 },
    { transport_epoch: 1.5 },
    { scene_fingerprint: "blob:private" },
    { owner_clip_id: "https://private.invalid/media" },
    { owner_clip_id: "x".repeat(129) },
    { state: "device_proven_audible" },
    { reason: "provider failure carrying private content" },
    { final_render_capability: "available" },
    { preview_capability: "future" },
    { gain: 1 },
    { lease_id: "private" },
  ])("rejects malformed or expanded status %j", (change) => {
    expect(() =>
      decodeEmbeddedAudioFollowerStatus({ ...status(), ...change }),
    ).toThrow("invalid_audio_status");
  });

  it("rejects a following claim without owner, scene, capability or matching reason", () => {
    for (const change of [
      { owner_clip_id: null },
      { scene_fingerprint: null },
      { preview_capability: "blocked" },
      { reason: "closed" },
    ])
      expect(() =>
        decodeEmbeddedAudioFollowerStatus({ ...status(), ...change }),
      ).toThrow("invalid_audio_status");
  });

  it("rejects extra keys, omitted keys, arrays and unbounded input", () => {
    const missing: Record<string, unknown> = status();
    delete missing.reason;
    for (const value of [null, [], missing, "x".repeat(4097)])
      expect(() => decodeEmbeddedAudioFollowerStatus(value)).toThrow(
        "invalid_audio_status",
      );
  });

  it("represents stopped and absent audio without an audible owner", () => {
    for (const reason of ["closed", "no_primary", "no_embedded_audio"])
      expect(
        decodeEmbeddedAudioFollowerStatus({
          ...status(),
          state: "silent",
          reason,
          owner_clip_id: null,
          scene_fingerprint:
            reason === "closed" ? null : status().scene_fingerprint,
        }).state,
      ).toBe("silent");
  });
});
