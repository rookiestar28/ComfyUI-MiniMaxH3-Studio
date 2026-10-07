import { describe, expect, it } from "vitest";
import {
  decodeNleAuthoringStateV2,
  encodeTimelineTransaction,
  encodeTimelineTransactionV2,
  NLE_AUTHORING_SCHEMA,
  NLE_AUTHORING_PROFILE_ID,
  NLE_OPERATION_PROFILE_ID_V2,
} from "../src/contracts/authoringWorkbenchCodec";
import {
  compositionContractFingerprint,
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import {
  planTitleInsertion,
  type TitleInsertionAuthority,
} from "../src/components/nle/nleTitleInsertion";
import {
  SMOKE_SHAPE,
  VIRTUALIZED_SHAPE,
  snapshotFixture,
  snapshotWire,
} from "./support/nleWorkspaceFixture";

const base = () => ({ ...snapshotFixture(SMOKE_SHAPE), clips: [] });
const input = (authority: TitleInsertionAuthority, overrides = {}) => ({
  busy: false,
  frame: 0,
  content: "Title",
  fontId: authority.assets.find((asset) => asset.kind === "font")!.assetId,
  ...overrides,
});

function emptyV2() {
  const source = snapshotWire(SMOKE_SHAPE);
  const material: Record<string, unknown> = {
    schema: NLE_AUTHORING_SCHEMA,
    profile_id: NLE_AUTHORING_PROFILE_ID,
    operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
    project_id: source.project_id,
    workspace_handle: source.workspace_handle,
    workspace_revision: source.workspace_revision,
    workspace_fingerprint: "",
    timeline_revision: source.timeline_revision,
    timeline_fingerprint: "",
    edit_capacity_frames: 3_600,
    content_end_exclusive: 0,
    assets: source.assets,
    tracks: (source.tracks as Record<string, unknown>[]).filter(
      (track) => track.kind === "primary_video",
    ),
    clips: [],
    audio_extension: source.audio_extension,
    blockers: [],
  };
  material.timeline_fingerprint = compositionContractFingerprint({
    operation_profile_id: material.operation_profile_id,
    edit_capacity_frames: material.edit_capacity_frames,
    content_end_exclusive: 0,
    tracks: material.tracks,
    clips: [],
    audio_extension: material.audio_extension,
  });
  material.workspace_fingerprint = compositionContractFingerprint({
    project_id: material.project_id,
    workspace_handle: material.workspace_handle,
    workspace_revision: material.workspace_revision,
    timeline_revision: material.timeline_revision,
    timeline_fingerprint: material.timeline_fingerprint,
  });
  material.authoring_fingerprint = compositionContractFingerprint(material);
  return decodeNleAuthoringStateV2(material);
}

describe("title insertion admission", () => {
  it("allocates after the track count despite valid order gaps and captures the planning authority", () => {
    const original = base();
    const authority = {
      ...original,
      tracks: original.tracks
        .filter((track) => track.kind !== "text_overlay")
        .map((track, index) => ({ ...track, order: index * 2 })),
    };
    const decision = planTitleInsertion(
      authority,
      input(authority, { frame: 18 }),
    );
    expect(decision.admitted).toBe(true);
    if (!decision.admitted) throw new Error("expected insertion");
    expect(decision.intent.commands).toHaveLength(2);
    expect(decision.intent.commands[0]).toMatchObject({
      kind: "create_track",
      payload: { order: 3, kind: "text_overlay" },
    });
    expect(decision.intent.commands[1]).toMatchObject({
      kind: "insert_title_clip",
      payload: { clip: { start_frame: 18, duration_frames: 24 } },
    });
    expect(decision.intent.capturedTimeline).toEqual({
      workspaceHandle: authority.workspaceHandle,
      workspaceRevision: authority.workspaceRevision,
      timelineRevision: authority.timelineRevision,
      timelineFingerprint: authority.timelineFingerprint,
    });
  });

  it("reuses the first ordered unlocked free text track, preserving a disabled track flag", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const title = original.clips.find((clip) => clip.text !== null)!;
    const primary = original.tracks[0]!;
    const authority = {
      ...original,
      tracks: [
        primary,
        {
          trackId: "text.later",
          kind: "text_overlay" as const,
          order: 4,
          enabled: true,
          locked: false,
        },
        {
          trackId: "text.locked",
          kind: "text_overlay" as const,
          order: 1,
          enabled: true,
          locked: true,
        },
        {
          trackId: "text.occupied",
          kind: "text_overlay" as const,
          order: 2,
          enabled: true,
          locked: false,
        },
        {
          trackId: "text.free",
          kind: "text_overlay" as const,
          order: 3,
          enabled: false,
          locked: false,
        },
      ],
      clips: [
        {
          ...title,
          trackId: "text.occupied",
          startFrame: 0,
          durationFrames: 24,
        },
      ],
    };
    const decision = planTitleInsertion(authority, input(authority));
    expect(decision.admitted).toBe(true);
    if (!decision.admitted) throw new Error("expected insertion");
    expect(decision.intent.commands).toHaveLength(1);
    expect(decision.intent.commands[0]).toMatchObject({
      payload: { clip: { track_id: "text.free" } },
    });
    expect(authority.tracks[4]!.enabled).toBe(false);
  });

  it("refuses exhausted track slots but admits a free text track within a full pool", () => {
    const original = base();
    const tracks = Array.from({ length: 8 }, (_, order) => ({
      trackId: `track.${order}`,
      kind:
        order === 0 ? ("primary_video" as const) : ("video_overlay" as const),
      order,
      enabled: true,
      locked: false,
    }));
    const authority = { ...original, tracks };
    expect(planTitleInsertion(authority, input(authority))).toEqual({
      admitted: false,
      reason: "track_limit",
    });
    const reusable = {
      ...authority,
      tracks: tracks.map((track, index) =>
        index === 7 ? { ...track, kind: "text_overlay" as const } : track,
      ),
    };
    const decision = planTitleInsertion(reusable, input(reusable));
    expect(decision.admitted && decision.intent.commands.length).toBe(1);
  });

  it("keeps empty V2 capacity independent of its zero extent and captures the authoring fingerprint", () => {
    const authority = emptyV2();
    expect(authority.contentEndExclusive).toBe(0);
    const decision = planTitleInsertion(authority, input(authority));
    if (!decision.admitted) throw new Error("expected insertion");
    expect(decision.intent.commands.map((command) => command.kind)).toEqual([
      "create_track",
      "insert_title_clip",
    ]);
    expect(decision.intent.commands[1]).toMatchObject({
      payload: { clip: { start_frame: 0, duration_frames: 24 } },
    });
    expect(decision.intent.capturedTimeline?.authoringFingerprint).toBe(
      authority.authoringFingerprint,
    );
    expect(() =>
      encodeTimelineTransactionV2({
        requestId: "title.empty",
        transactionId: "title.empty.tx",
        workspaceHandle: authority.workspaceHandle,
        expectedWorkspaceRevision: authority.workspaceRevision,
        expectedTimelineRevision: authority.timelineRevision,
        expectedTimelineFingerprint: authority.timelineFingerprint,
        expectedAuthoringFingerprint: authority.authoringFingerprint,
        commands: decision.intent.commands,
      }),
    ).not.toThrow();
  });

  it("truncates at the fixed V1 end and allocates identifiers without overwriting existing IDs", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const authority = {
      ...original,
      tracks: original.tracks
        .filter((track) => track.kind !== "text_overlay")
        .map((track, index) =>
          index === 1
            ? { ...track, trackId: `track-r${original.timelineRevision}-1` }
            : track,
        ),
      clips: [
        {
          ...original.clips[0]!,
          clipId: `clip-r${original.timelineRevision}-1`,
        },
      ],
    };
    const decision = planTitleInsertion(
      authority,
      input(authority, { frame: Number(authority.output.durationFrames) - 6 }),
    );
    if (!decision.admitted) throw new Error("expected insertion");
    expect(decision.intent.commands[0]).toMatchObject({
      payload: { track_id: `track-r${authority.timelineRevision}-2` },
    });
    expect(decision.intent.commands[1]).toMatchObject({
      payload: {
        clip: {
          clip_id: `clip-r${authority.timelineRevision}-2`,
          duration_frames: 6,
        },
      },
    });
  });

  it.each([NaN, Infinity, -Infinity, 1.5, Number.MAX_SAFE_INTEGER + 1])(
    "refuses an invalid frame %s",
    (frame) => {
      const authority = base();
      expect(
        planTitleInsertion(authority, input(authority, { frame })),
      ).toEqual({ admitted: false, reason: "unavailable_frame" });
    },
  );
  it("refuses current missing fonts, busy state, empty capacity and the actual clip ceiling", () => {
    const authority = base();
    expect(
      planTitleInsertion(authority, input(authority, { fontId: "removed" })),
    ).toEqual({ admitted: false, reason: "missing_font" });
    expect(
      planTitleInsertion(authority, input(authority, { busy: true })),
    ).toEqual({ admitted: false, reason: "busy" });
    expect(
      planTitleInsertion(
        { ...authority, output: { ...authority.output, durationFrames: 0 } },
        input(authority),
      ),
    ).toEqual({ admitted: false, reason: "insufficient_capacity" });
    const full = snapshotFixture(VIRTUALIZED_SHAPE);
    expect(full.clips).toHaveLength(128);
    expect(planTitleInsertion(full, input(full))).toEqual({
      admitted: false,
      reason: "clip_limit",
    });
  });

  const contents = [
    ["", false],
    ["a".repeat(2_049), false],
    ["a".repeat(2_048), true],
    ["😀".repeat(2_048), true],
    ["e\u0301", true],
    ["\ud800", false],
    ["\udfff", false],
    ["Title\rnext", false],
    ["Title\u0000", false],
    ["Title\tcard", true],
    [Array(32).fill("line").join("\n"), true],
    [Array(32).fill("line").join("\n") + "\n", false],
    [Array(31).fill("line").join("\n") + "\n", true],
    ...["\u0085", "\u2028", "\u2029"].map(
      (separator) => [Array(33).fill("line").join(separator), false] as const,
    ),
  ] as const;
  it.each(contents)(
    "content boundary %j is admitted=%s by both existing codecs",
    (content, admitted) => {
      const authority = base();
      const decision = planTitleInsertion(
        authority,
        input(authority, { content }),
      );
      expect(decision.admitted).toBe(admitted);
      if (!decision.admitted) {
        expect(decision.reason).toBe("invalid_content");
        return;
      }
      expect(() =>
        encodeTimelineTransaction({
          requestId: "title.content",
          transactionId: "title.content.tx",
          workspaceHandle: authority.workspaceHandle,
          expectedWorkspaceRevision: authority.workspaceRevision,
          expectedTimelineRevision: authority.timelineRevision,
          expectedTimelineFingerprint: authority.timelineFingerprint,
          commands: decision.intent.commands,
        }),
      ).not.toThrow();
      const clip = decision.intent.commands.at(-1)!.payload.clip as Record<
        string,
        unknown
      >;
      expect((clip.text as { content: string }).content).toBe(
        content.normalize("NFC"),
      );
      const wire = snapshotWire(SMOKE_SHAPE);
      wire.clips = [clip];
      wire.public_fingerprint = publicCompositionFingerprint(wire);
      expect(() => decodePublicCompositionSnapshot(wire)).not.toThrow();
    },
  );
});
