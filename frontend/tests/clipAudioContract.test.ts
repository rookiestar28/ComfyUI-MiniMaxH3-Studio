import { describe, expect, it } from "vitest";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import admission from "../../tests/fixtures/m25_77_clip_audio_admission_rows_v1.json";
import table from "../../tests/fixtures/m25_77_clip_audio_wire_rows_v1.json";
import { encodeTimelineTransaction } from "../src/contracts/authoringWorkbenchCodec";
import {
  CompositionContractError,
  IDENTITY_CLIP_AUDIO,
  decodeCompositionClip,
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import {
  clipAudioKeys,
  compositionClipKeys,
} from "../src/contracts/generatedSurface";

// The clip wire's audio member. The rows are the backend's own table
// (`tests/test_m25_77_clip_audio_contract.py` reads the same file), so the two decoders answer
// one list of cases rather than two lists that agree by intention.

type Row = Readonly<{
  name: string;
  duration_frames: number;
  absent?: boolean;
  audio?: unknown;
  result?: Readonly<Record<string, number | boolean>>;
  refusal?: string;
}>;

const rows = table.rows as readonly Row[];

type AdmissionRow = Readonly<{
  name: string;
  clip_id: string;
  asset_id?: string;
  embedded_audio?: string;
  result: string;
}>;

const admissionRows = admission.rows as readonly AdmissionRow[];

function admissionWire(
  row: AdmissionRow,
  withAudio: boolean,
): Record<string, unknown> {
  const wire = structuredClone(fixture.snapshot) as Record<string, unknown>;
  const clip = (wire.clips as Array<Record<string, unknown>>).find(
    (item) => item.clip_id === row.clip_id,
  )!;
  if (row.asset_id !== undefined) clip.asset_id = row.asset_id;
  if (row.embedded_audio !== undefined)
    (wire.assets as Array<Record<string, unknown>>).find(
      (item) => item.asset_id === clip.asset_id,
    )!.embedded_audio = row.embedded_audio;
  if (withAudio) clip.audio = structuredClone(admission.audio);
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return wire;
}

function clipWire(row: Row): Record<string, unknown> {
  const clip = structuredClone(
    fixture.snapshot.clips.find((item) => item.clip_id === "clip-main"),
  ) as Record<string, unknown>;
  clip.duration_frames = row.duration_frames;
  if (row.absent !== true) clip.audio = structuredClone(row.audio);
  return clip;
}

function camel(
  result: Readonly<Record<string, number | boolean>>,
): Record<string, number | boolean> {
  return {
    gainMb: result.gain_mb as number,
    muted: result.muted as boolean,
    fadeInFrames: result.fade_in_frames as number,
    fadeOutFrames: result.fade_out_frames as number,
  };
}

function refusalCode(action: () => unknown): string {
  try {
    action();
  } catch (error) {
    if (error instanceof CompositionContractError) return error.code;
    throw error;
  }
  return "accepted";
}

describe("clip audio member", () => {
  it.each(rows.map((row) => [row.name, row] as const))(
    "decodes the shared row %s as the backend does",
    (_name, row) => {
      if (row.refusal !== undefined) {
        expect(refusalCode(() => decodeCompositionClip(clipWire(row)))).toBe(
          row.refusal,
        );
        return;
      }
      const clip = decodeCompositionClip(clipWire(row));
      expect({ ...clip.audio }).toEqual(camel(row.result!));
    },
  );

  it.each(rows.map((row) => [row.name, row] as const))(
    "validates the shared row %s inside a command's clip",
    (_name, row) => {
      const wire = clipWire(row);
      wire.clip_id = "clip-inserted";
      const encode = () =>
        encodeTimelineTransaction({
          requestId: "req-clip-audio",
          transactionId: "tx-clip-audio",
          workspaceHandle: String(fixture.snapshot.workspace_handle),
          expectedWorkspaceRevision: Number(
            fixture.snapshot.workspace_revision,
          ),
          expectedTimelineRevision: Number(fixture.snapshot.timeline_revision),
          expectedTimelineFingerprint: String(
            fixture.snapshot.timeline_fingerprint,
          ),
          commands: [{ kind: "insert_asset_clip", payload: { clip: wire } }],
        });
      if (row.refusal !== undefined) expect(encode).toThrow();
      else expect(encode().commands[0]?.payload.clip).toEqual(wire);
    },
  );

  it.each([null, undefined, 7, "clip"])(
    "refuses a clip wire that is not an object (%s) with the contract's code",
    (value) => {
      // Whether the optional member is present is asked of an object only: asked of null or
      // undefined it would throw before the decoder's own refusal.
      expect(refusalCode(() => decodeCompositionClip(value))).toBe(
        "invalid_contract",
      );
    },
  );

  it("freezes the identity value and every decoded member and clip", () => {
    expect(Object.isFrozen(IDENTITY_CLIP_AUDIO)).toBe(true);
    const row = rows.find((item) => item.name === "every_value_at_once")!;
    const clip = decodeCompositionClip(clipWire(row));
    expect(Object.isFrozen(clip)).toBe(true);
    expect(Object.isFrozen(clip.audio)).toBe(true);
    expect(clip.audio).not.toBe(IDENTITY_CLIP_AUDIO);
  });

  it("decodes every clip of the accepted composition to identity audio", () => {
    const snapshot = decodePublicCompositionSnapshot(
      structuredClone(fixture.snapshot),
    );
    expect(snapshot.publicFingerprint).toBe(
      fixture.snapshot.public_fingerprint,
    );
    for (const clip of snapshot.clips)
      expect(clip.audio).toEqual(IDENTITY_CLIP_AUDIO);
    expect({ ...IDENTITY_CLIP_AUDIO }).toEqual({
      gainMb: 0,
      muted: false,
      fadeInFrames: 0,
      fadeOutFrames: 0,
    });
  });

  it.each(admissionRows.map((row) => [row.name, row] as const))(
    "admits a member on the admission row %s as the backend does",
    (_name, row) => {
      // The same composition without the member is admitted, so a refusal is the member's own.
      expect(
        refusalCode(() =>
          decodePublicCompositionSnapshot(admissionWire(row, false)),
        ),
      ).toBe("accepted");
      const wire = admissionWire(row, true);
      expect(refusalCode(() => decodePublicCompositionSnapshot(wire))).toBe(
        row.result,
      );
      if (row.result !== "accepted") return;
      const snapshot = decodePublicCompositionSnapshot(wire);
      const clip = snapshot.clips.find((item) => item.clipId === row.clip_id)!;
      expect({ ...clip.audio }).toEqual(camel(admission.audio));
      expect(snapshot.publicFingerprint).toBe(wire.public_fingerprint);
    },
  );

  it("reads the member's keys from the generated surface", () => {
    expect([...clipAudioKeys]).toEqual([
      "fade_in_frames",
      "fade_out_frames",
      "gain_mb",
      "muted",
    ]);
    expect(compositionClipKeys).toContain("audio");
  });
});
