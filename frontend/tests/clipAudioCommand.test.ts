import { describe, expect, it } from "vitest";

import compositionFixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import { encodeTimelineTransaction } from "../src/contracts/authoringWorkbenchCodec";
import {
  CLIP_AUDIO_FADE_MAX_FRAMES,
  CLIP_AUDIO_GAIN_MAX_MB,
  CLIP_AUDIO_GAIN_MIN_MB,
  IDENTITY_CLIP_AUDIO,
  NLE_OPERATION_IDS,
} from "../src/contracts/compositionCodec";
import {
  IDENTITY_CLIP_AUDIO_WIRE,
  REBASABLE_KINDS,
  build,
  clipAudioWire,
  rebasable,
} from "../src/components/nle/nleCommandBuilders";

// `set_clip_audio`: a video clip's own gain, mute and fades, as one command. The backend's own
// rows are `tests/test_m25_77_clip_audio_command.py`; this file holds the browser's half -- the
// closed vocabulary, the wire shape it sends and the rebase set it shares with the history.

const snapshot = compositionFixture.snapshot as Record<string, unknown>;

function transaction(commands: readonly { kind: string; payload: object }[]) {
  return encodeTimelineTransaction({
    requestId: "req-clip-audio",
    transactionId: "tx-clip-audio",
    workspaceHandle: String(snapshot.workspace_handle),
    expectedWorkspaceRevision: Number(snapshot.workspace_revision),
    expectedTimelineRevision: Number(snapshot.timeline_revision),
    expectedTimelineFingerprint: String(snapshot.timeline_fingerprint),
    commands: commands as never,
  });
}

const PAYLOAD = Object.freeze({
  clip_id: "clip-main",
  gain_mb: -600,
  muted: false,
  fade_in_frames: 12,
  fade_out_frames: 12,
});

describe("set_clip_audio command", () => {
  it("is the thirty-fourth operation, after set_effect", () => {
    expect(NLE_OPERATION_IDS).toHaveLength(34);
    expect(NLE_OPERATION_IDS.indexOf("set_clip_audio")).toBe(
      NLE_OPERATION_IDS.indexOf("set_effect") + 1,
    );
  });

  it("sends exactly its five members, each inside the member's bounds", () => {
    const accepted = transaction([
      { kind: "set_clip_audio", payload: PAYLOAD },
    ]);
    expect(accepted.commands[0]).toEqual({
      kind: "set_clip_audio",
      payload: PAYLOAD,
    });
    for (const [member, value] of [
      ["gain_mb", CLIP_AUDIO_GAIN_MIN_MB],
      ["gain_mb", CLIP_AUDIO_GAIN_MAX_MB],
      ["fade_in_frames", CLIP_AUDIO_FADE_MAX_FRAMES],
      ["fade_in_frames", 0],
      ["fade_out_frames", CLIP_AUDIO_FADE_MAX_FRAMES],
      ["fade_out_frames", 0],
      ["muted", true],
    ] as const)
      expect(
        transaction([
          { kind: "set_clip_audio", payload: { ...PAYLOAD, [member]: value } },
        ]).commands[0]!.payload[member],
        member,
      ).toBe(value);
    for (const [member, value, refusal] of [
      ["gain_mb", CLIP_AUDIO_GAIN_MIN_MB - 1, /gain_mb/],
      ["gain_mb", CLIP_AUDIO_GAIN_MAX_MB + 1, /gain_mb/],
      // A fraction never reaches the member check: the payload is not canonical JSON.
      ["gain_mb", -600.5, /canonical JSON/],
      ["gain_mb", "-600", /gain_mb/],
      ["fade_in_frames", -1, /fade_in_frames/],
      ["fade_in_frames", CLIP_AUDIO_FADE_MAX_FRAMES + 1, /fade_in_frames/],
      ["fade_out_frames", -1, /fade_out_frames/],
      ["fade_out_frames", CLIP_AUDIO_FADE_MAX_FRAMES + 1, /fade_out_frames/],
      ["muted", 0, /muted/],
      ["muted", null, /muted/],
      ["clip_id", "bad handle", /clip_id/],
    ] as const)
      expect(
        () =>
          transaction([
            {
              kind: "set_clip_audio",
              payload: { ...PAYLOAD, [member]: value },
            },
          ]),
        `${member}=${String(value)}`,
      ).toThrow(refusal);
    const { fade_out_frames: _dropped, ...missing } = PAYLOAD;
    expect(() =>
      transaction([{ kind: "set_clip_audio", payload: missing }]),
    ).toThrow(/payload.*closed/);
    expect(() =>
      transaction([
        { kind: "set_clip_audio", payload: { ...PAYLOAD, pan: 0 } },
      ]),
    ).toThrow(/payload.*closed/);
  });

  // GUARD: name the complete deferred vocabulary; sampling misses an omitted refusal.
  it.each([
    "create_audio_track",
    "insert_audio",
    "import_audio",
    "replace_audio",
    "link_audio",
    "unlink_audio",
    "set_gain",
    "set_pan",
    "set_mute",
    "set_solo",
    "set_envelope",
    "set_waveform",
    "mix_audio",
    "h3.authoring.audio.command.v1.gain",
  ])("leaves every independent-audio name deferred: %s", (kind) => {
    expect(() =>
      transaction([{ kind, payload: { clip_id: "clip-main" } }]),
    ).toThrow(/audio_editing_deferred/);
  });

  it("builds the exact wire from a clip's member and rebases with the property edits", () => {
    expect(
      build.setClipAudio("clip-main", {
        gain_mb: -600,
        muted: false,
        fade_in_frames: 12,
        fade_out_frames: 12,
      }),
    ).toEqual({ kind: "set_clip_audio", payload: PAYLOAD });
    expect(clipAudioWire(undefined)).toEqual({
      gain_mb: 0,
      muted: false,
      fade_in_frames: 0,
      fade_out_frames: 0,
    });
    expect(
      clipAudioWire({
        audio: {
          ...IDENTITY_CLIP_AUDIO,
          gainMb: -600,
          muted: true,
          fadeOutFrames: 6,
        },
      } as never),
    ).toEqual({
      gain_mb: -600,
      muted: true,
      fade_in_frames: 0,
      fade_out_frames: 6,
    });
    expect(REBASABLE_KINDS.has("set_clip_audio")).toBe(true);
    const command = build.setClipAudio("clip-main", clipAudioWire(undefined));
    expect(rebasable([command])).toBe(true);
    const rebase = transaction([
      {
        kind: "rebase_transaction",
        payload: {
          base_timeline_fingerprint: snapshot.timeline_fingerprint,
          commands: [{ kind: "set_clip_audio", payload: PAYLOAD }],
        },
      },
    ]);
    expect(rebase.commands[0]!.payload.commands).toEqual([
      { kind: "set_clip_audio", payload: PAYLOAD },
    ]);
  });

  it("decodes a rebased set_clip_audio as a command of its own and keeps the rebase set closed", () => {
    const rebase = (commands: readonly { kind: string; payload: object }[]) =>
      transaction([
        {
          kind: "rebase_transaction",
          payload: {
            base_timeline_fingerprint: snapshot.timeline_fingerprint,
            commands,
          },
        },
      ]);
    // The nested command is decoded, not copied: its members are checked as at the top level.
    expect(() =>
      rebase([
        {
          kind: "set_clip_audio",
          payload: { ...PAYLOAD, gain_mb: CLIP_AUDIO_GAIN_MAX_MB + 1 },
        },
      ]),
    ).toThrow(/gain_mb/);
    // A command that changes a clip's extent is never rebased, with or without the audio edit.
    expect(() =>
      rebase([
        { kind: "set_clip_audio", payload: PAYLOAD },
        {
          kind: "trim_clip",
          payload: { clip_id: "clip-main", edge: "end", delta_frames: -1 },
        },
      ]),
    ).toThrow(/rebase_conflict/);
  });

  it("keeps every list and value it shares frozen", () => {
    expect(Object.isFrozen(NLE_OPERATION_IDS)).toBe(true);
    expect(Object.isFrozen(REBASABLE_KINDS)).toBe(true);
    expect(Object.isFrozen(IDENTITY_CLIP_AUDIO_WIRE)).toBe(true);
    expect(Object.isFrozen(build)).toBe(true);
    const wire = clipAudioWire({
      audio: { ...IDENTITY_CLIP_AUDIO, gainMb: -600 },
    } as never);
    expect(wire).not.toBe(IDENTITY_CLIP_AUDIO_WIRE);
    expect(Object.isFrozen(wire)).toBe(true);
  });
});
