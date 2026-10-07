import { describe, expect, it } from "vitest";
import compositionFixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";

import {
  AUTHORING_ACTION_SCHEMA,
  TIMELINE_HISTORY_PROJECTION_SCHEMA,
  TIMELINE_RECEIPT_SCHEMA,
  TIMELINE_TRANSACTION_SCHEMA,
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
  TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
  authoringMoveGroupPayload,
  decodeAuthoringProjection,
  decodeNleAuthoringStateV2,
  decodeTimelineHistoryProjectionV2,
  decodeAuthoringSnap,
  decodeTimelineHistoryProjection,
  decodeTimelineReceipt,
  encodeTimelineTransaction,
  encodeTimelineTransactionV2,
  encodeAuthoringAction,
} from "../src/contracts/authoringWorkbenchCodec";
import {
  FP,
  capacityWire,
  projectionWire,
  snapWire,
  sourceWire,
} from "./support/authoringFixture";
import {
  NLE_OPERATION_IDS,
  compositionContractFingerprint,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";

function compositionSnapshotWire(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(compositionFixture.snapshot)) as Record<
    string,
    unknown
  >;
}

function receiptWire(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const snapshot = compositionSnapshotWire();
  return {
    schema: "h3.context.timeline_receipt.v1",
    request_id: "req-history-1",
    transaction_id: "tx-history-1",
    workspace_handle: snapshot.workspace_handle,
    before_workspace_revision: 6,
    after_workspace_revision: snapshot.workspace_revision,
    before_workspace_fingerprint: FP,
    after_workspace_fingerprint: snapshot.workspace_fingerprint,
    before_timeline_revision: 10,
    after_timeline_revision: snapshot.timeline_revision,
    before_timeline_fingerprint: FP,
    after_timeline_fingerprint: snapshot.timeline_fingerprint,
    commands: [{ kind: "select_clips", payload: { clip_ids: ["clip-main"] } }],
    affected_ids: ["clip-main"],
    inverse: {
      kind: "restore_transaction_state",
      history_cursor: `h3.context.timeline_history_cursor.v1:1:${"a".repeat(64)}`,
    },
    history_cursor: `h3.context.timeline_history_cursor.v1:1:${"a".repeat(64)}`,
    selection: ["clip-main"],
    snapshot,
    ...overrides,
  };
}

function historyProjectionWire(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const snapshot = compositionSnapshotWire();
  return {
    schema: "h3.context.timeline_history_projection.v1",
    workspace_handle: snapshot.workspace_handle,
    snapshot,
    selection: ["clip-main"],
    undo_cursor: `h3.context.timeline_history_cursor.v1:1:${"a".repeat(64)}`,
    redo_cursor: null,
    rejection: null,
    ...overrides,
  };
}

function emptyAuthoringStateWire(): Record<string, unknown> {
  const audioExtension = {
    schema: "h3.authoring.independent_audio_extension.v1",
    track_profile: "none_v1",
    command_namespace: "h3.authoring.audio.command.v1",
    command_members: [],
    preview_edit_capability: "unsupported",
    final_render_edit_capability: "unsupported",
    embedded_renderer_variant: "EmbeddedAudioSpanV1",
    independent_audio_renderer_variant: "none_v1",
    reason: "audio_editing_deferred",
  };
  const material: Record<string, unknown> = {
    schema: NLE_AUTHORING_SCHEMA,
    profile_id: NLE_AUTHORING_PROFILE_ID,
    operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
    project_id: "project.1",
    workspace_handle: "authoring-abc123",
    workspace_revision: 3,
    workspace_fingerprint: "",
    timeline_revision: 4,
    timeline_fingerprint: "",
    edit_capacity_frames: 3_600,
    content_end_exclusive: 0,
    assets: [],
    tracks: [
      {
        track_id: "track.primary",
        kind: "primary_video",
        order: 0,
        enabled: true,
        locked: false,
      },
    ],
    clips: [],
    audio_extension: audioExtension,
    blockers: [],
  };
  material.timeline_fingerprint = compositionContractFingerprint({
    operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
    edit_capacity_frames: 3_600,
    content_end_exclusive: 0,
    tracks: material.tracks,
    clips: [],
    audio_extension: audioExtension,
  });
  material.workspace_fingerprint = compositionContractFingerprint({
    project_id: material.project_id,
    workspace_handle: material.workspace_handle,
    workspace_revision: material.workspace_revision,
    timeline_revision: material.timeline_revision,
    timeline_fingerprint: material.timeline_fingerprint,
  });
  material.authoring_fingerprint = compositionContractFingerprint(material);
  return material;
}

describe("authoring projection decode", () => {
  it("decodes empty V2 history without inventing a V1 render snapshot", () => {
    const authoring = emptyAuthoringStateWire();
    const state = decodeNleAuthoringStateV2(authoring);
    const history = decodeTimelineHistoryProjectionV2({
      schema: TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
      workspace_handle: state.workspaceHandle,
      authoring,
      render_snapshot: null,
      selection: [],
      undo_cursor: null,
      redo_cursor: null,
      rejection: null,
    });
    expect(state.contentEndExclusive).toBe(0);
    expect(history.renderSnapshot).toBeNull();
    expect(history.renderSnapshot).toBeNull();
    expect(history.authoring?.authoringFingerprint).toBe(
      state.authoringFingerprint,
    );
  });

  it("rejects a fabricated V1 snapshot for empty V2 authoring", () => {
    const authoring = emptyAuthoringStateWire();
    expect(() =>
      decodeTimelineHistoryProjectionV2({
        schema: TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
        workspace_handle: authoring.workspace_handle,
        authoring,
        render_snapshot: compositionSnapshotWire(),
        selection: [],
        undo_cursor: null,
        redo_cursor: null,
        rejection: null,
      }),
    ).toThrow(/presence does not match content extent/);
  });

  it("encodes a V2 transaction against authoring and timeline fingerprints", () => {
    const state = decodeNleAuthoringStateV2(emptyAuthoringStateWire());
    expect(
      encodeTimelineTransactionV2({
        requestId: "request.v2",
        transactionId: "tx.v2",
        workspaceHandle: state.workspaceHandle,
        expectedWorkspaceRevision: state.workspaceRevision,
        expectedTimelineRevision: state.timelineRevision,
        expectedTimelineFingerprint: state.timelineFingerprint,
        expectedAuthoringFingerprint: state.authoringFingerprint,
        commands: [{ kind: "select_clips", payload: { clip_ids: [] } }],
      }),
    ).toMatchObject({
      schema: "h3.context.timeline_transaction.v2",
      authoring_schema: NLE_AUTHORING_SCHEMA,
      expected_authoring_fingerprint: state.authoringFingerprint,
    });
  });

  it("encodes V2 history commands with the V2 branch cursor schema", () => {
    const state = decodeNleAuthoringStateV2(emptyAuthoringStateWire());
    const cursor = `h3.context.timeline_history_cursor.v2:1:${"a".repeat(64)}`;
    expect(
      encodeTimelineTransactionV2({
        requestId: "request.undo.v2",
        transactionId: "tx.undo.v2",
        workspaceHandle: state.workspaceHandle,
        expectedWorkspaceRevision: state.workspaceRevision,
        expectedTimelineRevision: state.timelineRevision,
        expectedTimelineFingerprint: state.timelineFingerprint,
        expectedAuthoringFingerprint: state.authoringFingerprint,
        commands: [{ kind: "undo", payload: { history_cursor: cursor } }],
      }).commands,
    ).toEqual([{ kind: "undo", payload: { history_cursor: cursor } }]);
  });

  it.each([
    "present_bound",
    "absent",
    "unavailable",
    "excluded_overlay_policy",
  ])(
    "preserves %s source facts in historical NLE state and result readers",
    (disposition) => {
      const snapshot = compositionSnapshotWire();
      const assets = snapshot.assets as Array<Record<string, unknown>>;
      assets[1]!.embedded_audio = disposition;
      assets[1]!.source_sample_count =
        disposition === "present_bound" ? 48000 : null;
      snapshot.public_fingerprint = publicCompositionFingerprint(snapshot);
      const before = structuredClone(snapshot);
      const receipt = decodeTimelineReceipt(receiptWire({ snapshot }));
      const history = decodeTimelineHistoryProjection(
        historyProjectionWire({ snapshot }),
      );
      expect(receipt.snapshot.assets[1]?.embeddedAudio).toBe(disposition);
      expect(history.snapshot.assets[1]?.embeddedAudio).toBe(disposition);
      expect(receipt.snapshot.publicFingerprint).toBe(
        snapshot.public_fingerprint,
      );
      expect(history.snapshot.publicFingerprint).toBe(
        snapshot.public_fingerprint,
      );
      expect(snapshot).toEqual(before);
      expect(Object.isFrozen(receipt.snapshot.assets[1])).toBe(true);
    },
  );

  it("decodes a full valid projection into frozen camelCase structures", () => {
    const projection = decodeAuthoringProjection(projectionWire());
    expect(projection.workspaceHandle).toBe("authoring-abc123");
    expect(projection.taskMode).toBe("t2va");
    expect(projection.registryFingerprint).toBe(FP);
    expect(projection.reference.revision).toBe(4);
    expect(projection.reference.sources).toHaveLength(4);
    expect(projection.reference.sources[0]?.label).toBe("<Picture 1>");
    expect(projection.reference.sources[0]?.preview.reason).toBe("not_bound");
    expect(projection.reference.canonical.map((row) => row.sourceId)).toEqual([
      "img-1",
      "vid-1",
      "aud-2",
    ]);
    expect(projection.reference.soundtracks[0]?.derivedState).toBe(
      "blocked_unknown",
    );
    expect(projection.reference.queueBlockers[0]?.code).toBe("blocked_unknown");
    expect(projection.reference.capacity.aggregateRemaining).toBe(9);
    expect(projection.reference.capacity.maxDurationMilliseconds).toBe(149_687);
    expect(projection.availability.producer).toBeNull();
    expect(projection.timeline.revision).toBe(2);
    expect(projection.timeline.profile.audioPeriodFrames).toBe(3);
    expect(projection.timeline.clips[0]?.clipId).toBe("clip-1");
    expect(projection.rejection).toBeNull();
    expect(Object.isFrozen(projection)).toBe(true);
    expect(Object.isFrozen(projection.reference.sources)).toBe(true);
    expect(Object.isFrozen(projection.timeline.clips[0])).toBe(true);
  });

  it("decodes a rejection code when the backend refused the action", () => {
    const projection = decodeAuthoringProjection(
      projectionWire({ rejection: { code: "revision_conflict" } }),
    );
    expect(projection.rejection?.code).toBe("revision_conflict");
  });

  it("rejects an open object with an extra key", () => {
    expect(() =>
      decodeAuthoringProjection({ ...projectionWire(), surprise: true }),
    ).toThrow(/closed/);
  });

  it("rejects a missing key", () => {
    const wire = projectionWire();
    delete (wire as Record<string, unknown>).rejection;
    expect(() => decodeAuthoringProjection(wire)).toThrow(/closed/);
  });

  it("rejects an unsupported schema", () => {
    expect(() =>
      decodeAuthoringProjection(projectionWire({ schema: "h3.other.v2" })),
    ).toThrow(/schema is unsupported/);
  });

  it("rejects a malformed registry fingerprint", () => {
    expect(() =>
      decodeAuthoringProjection(
        projectionWire({ registry_fingerprint: "sha256:XYZ" }),
      ),
    ).toThrow(/registry fingerprint/);
  });

  it("rejects a source label outside the exact canonical shape", () => {
    const wire = projectionWire();
    const reference = (wire as { reference: Record<string, unknown> })
      .reference;
    reference.sources = [sourceWire({ label: "<Video 0>" })];
    expect(() => decodeAuthoringProjection(wire)).toThrow(/source label/);
  });

  it("rejects a label on a source that is not admitted", () => {
    const wire = projectionWire();
    const reference = (wire as { reference: Record<string, unknown> })
      .reference;
    reference.sources = [sourceWire({ admitted: false, admissible: false })];
    expect(() => decodeAuthoringProjection(wire)).toThrow(
      /not admitted cannot carry a label/,
    );
  });

  it("defaults an M25-00 source row without preview authority to not_bound", () => {
    const wire = projectionWire();
    const reference = (wire as { reference: Record<string, unknown> })
      .reference;
    const source = sourceWire();
    delete source.preview;
    reference.sources = [source];
    const decoded = decodeAuthoringProjection(wire);
    expect(decoded.reference.sources[0]?.preview).toEqual({
      schema: "h3.context.authoring_source_preview.capability.v1",
      available: false,
      reason: "not_bound",
    });
  });

  it("rejects an open preview capability that could leak authority detail", () => {
    const wire = projectionWire();
    const reference = (wire as { reference: Record<string, unknown> })
      .reference;
    reference.sources = [
      sourceWire({
        preview: {
          schema: "h3.context.authoring_source_preview.capability.v1",
          available: true,
          reason: null,
          path: "private.mp4",
        },
      }),
    ];
    expect(() => decodeAuthoringProjection(wire)).toThrow(/capability.*closed/);
  });

  it("rejects a canonical pairing on a non-audio asset", () => {
    const wire = projectionWire();
    const reference = (wire as { reference: Record<string, unknown> })
      .reference;
    reference.canonical = [
      {
        source_id: "img-1",
        kind: "image",
        label: "<Picture 1>",
        paired_with: "<Video 1>",
      },
    ];
    expect(() => decodeAuthoringProjection(wire)).toThrow(
      /only an audio asset can be paired/,
    );
  });

  it("rejects a capacity that advertises an impossible per-kind maximum", () => {
    const wire = projectionWire();
    const reference = (wire as { reference: Record<string, unknown> })
      .reference;
    reference.capacity = { ...capacityWire(), image_remaining: 10 };
    expect(() => decodeAuthoringProjection(wire)).toThrow(/impossible maximum/);
  });

  it("rejects a non-integer frame figure", () => {
    const wire = projectionWire();
    const timeline = (wire as { timeline: Record<string, unknown> }).timeline;
    timeline.clips = [
      {
        clip_id: "clip-1",
        asset_id: "vid-1",
        kind: "video",
        lane: 0,
        start_frame: 1.5,
        frames: 12,
        source_start_frame: 0,
        envelope: [],
      },
    ];
    expect(() => decodeAuthoringProjection(wire)).toThrow(/clip start/);
  });

  it("rejects an unbounded row count", () => {
    const wire = projectionWire();
    const timeline = (wire as { timeline: Record<string, unknown> }).timeline;
    timeline.selection = Array.from({ length: 65 }, (_, i) => `clip-${i}`);
    expect(() => decodeAuthoringProjection(wire)).toThrow(
      /authoring selection/,
    );
  });
});

describe("authoring snap decode", () => {
  it("decodes ordered snap candidates", () => {
    const snap = decodeAuthoringSnap(snapWire());
    expect(snap.workspaceHandle).toBe("authoring-abc123");
    expect(snap.timelineRevision).toBe(2);
    expect(snap.candidates.map((row) => row.kind)).toEqual([
      "grid",
      "clip_boundary",
    ]);
  });

  it("rejects an unknown candidate kind", () => {
    const wire = snapWire();
    (wire as { candidates: unknown[] }).candidates = [
      { frame: 51, kind: "magnet", distance: 1 },
    ];
    expect(() => decodeAuthoringSnap(wire)).toThrow(/candidate kind/);
  });

  it("rejects an open snap object", () => {
    expect(() => decodeAuthoringSnap({ ...snapWire(), extra: 1 })).toThrow(
      /closed/,
    );
  });
});

describe("authoring action encode", () => {
  it("emits the closed action envelope", () => {
    const encoded = encodeAuthoringAction("req-1", "read_projection", {
      workspace_handle: "authoring-abc123",
    });
    expect(encoded.schema).toBe(AUTHORING_ACTION_SCHEMA);
    expect(encoded.request_id).toBe("req-1");
    expect(encoded.action).toBe("read_projection");
    expect(Object.isFrozen(encoded)).toBe(true);
    expect(Object.isFrozen(encoded.payload)).toBe(true);
  });

  it("rejects a request id outside the identifier shape", () => {
    expect(() => encodeAuthoringAction("", "read_projection", {})).toThrow(
      /request id/,
    );
    expect(() =>
      encodeAuthoringAction("bad id", "read_projection", {}),
    ).toThrow(/request id/);
  });

  it("rejects an invalid timeline history workspace handle before transport", () => {
    expect(() =>
      encodeAuthoringAction("req-history-read", "read_timeline_history", {
        workspace_handle: "bad handle",
      }),
    ).toThrow(/timeline history workspace handle/);
  });

  it("encodes only the closed timeline history initialization CAS", () => {
    const encoded = encodeAuthoringAction(
      "req-history-init",
      "initialize_timeline_history",
      {
        workspace_handle: "workspace-fixture",
        expected_reference_revision: 4,
        expected_timeline_revision: 1,
        authoring_schema: NLE_AUTHORING_SCHEMA,
        profile_id: NLE_AUTHORING_PROFILE_ID,
        operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
      },
    );
    expect(encoded.payload).toEqual({
      workspace_handle: "workspace-fixture",
      expected_reference_revision: 4,
      expected_timeline_revision: 1,
      authoring_schema: NLE_AUTHORING_SCHEMA,
      profile_id: NLE_AUTHORING_PROFILE_ID,
      operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
    });
    expect(Object.isFrozen(encoded.payload)).toBe(true);

    for (const payload of [
      {
        workspace_handle: "workspace-fixture",
        expected_reference_revision: 4,
      },
      {
        workspace_handle: "workspace-fixture",
        expected_reference_revision: 4,
        expected_timeline_revision: 1,
        browser_snapshot: {},
      },
      {
        workspace_handle: "workspace-fixture",
        expected_reference_revision: 0,
        expected_timeline_revision: 1,
      },
      {
        workspace_handle: "workspace-fixture",
        expected_reference_revision: 4,
        expected_timeline_revision: 1_000_001,
      },
      {
        workspace_handle: "bad handle",
        expected_reference_revision: 4,
        expected_timeline_revision: 1,
      },
      {
        workspace_handle: "workspace-fixture",
        expected_reference_revision: true,
        expected_timeline_revision: 1,
      },
    ]) {
      expect(() =>
        encodeAuthoringAction(
          "req-history-init-invalid",
          "initialize_timeline_history",
          payload,
        ),
      ).toThrow(/timeline history initialization/);
    }
  });

  it("maps the exact move_group payload while retaining legacy set_envelope encoding", () => {
    const payload = authoringMoveGroupPayload({
      workspaceHandle: "authoring-abc123",
      expectedTimelineRevision: 7,
      clipIds: ["clip-1", "clip-2"],
      deltaFrames: 51,
    });
    expect(payload).toEqual({
      workspace_handle: "authoring-abc123",
      expected_timeline_revision: 7,
      clip_ids: ["clip-1", "clip-2"],
      delta_frames: 51,
    });
    expect(Object.isFrozen(payload)).toBe(true);
    expect(Object.isFrozen(payload.clip_ids)).toBe(true);
    expect(
      encodeAuthoringAction("req-group", "move_group", payload).action,
    ).toBe("move_group");
    expect(
      encodeAuthoringAction("req-envelope", "set_envelope", {
        workspace_handle: "authoring-abc123",
        expected_timeline_revision: 7,
        clip_id: "clip-1",
        points: [],
      }).action,
    ).toBe("set_envelope");
  });
});

describe("M25-11 timeline transaction codec", () => {
  it("encodes the exact closed transaction and freezes the caller-visible tree", () => {
    const snapshot = compositionSnapshotWire();
    const transaction = encodeTimelineTransaction({
      requestId: "req-history-1",
      transactionId: "tx-history-1",
      workspaceHandle: String(snapshot.workspace_handle),
      expectedWorkspaceRevision: Number(snapshot.workspace_revision),
      expectedTimelineRevision: Number(snapshot.timeline_revision),
      expectedTimelineFingerprint: String(snapshot.timeline_fingerprint),
      commands: [
        { kind: "select_clips", payload: { clip_ids: ["clip-main"] } },
      ],
    });
    expect(transaction).toEqual({
      schema: TIMELINE_TRANSACTION_SCHEMA,
      request_id: "req-history-1",
      transaction_id: "tx-history-1",
      workspace_handle: "workspace-fixture",
      expected_workspace_revision: 7,
      expected_timeline_revision: 11,
      expected_timeline_fingerprint: snapshot.timeline_fingerprint,
      commands: [
        { kind: "select_clips", payload: { clip_ids: ["clip-main"] } },
      ],
    });
    expect(Object.isFrozen(transaction)).toBe(true);
    expect(Object.isFrozen(transaction.commands[0]?.payload)).toBe(true);
    expect(
      encodeAuthoringAction(
        "req-history-1",
        "apply_timeline_transaction",
        transaction,
      ).action,
    ).toBe("apply_timeline_transaction");
    expect(() =>
      encodeAuthoringAction("req-history-1", "apply_timeline_transaction", {
        ...transaction,
        browser_snapshot: {},
      }),
    ).toThrow(/transaction.*closed/);
  });

  it("rejects unknown payload members and every independent-audio command", () => {
    const snapshot = compositionSnapshotWire();
    const base = {
      requestId: "req-history-1",
      transactionId: "tx-history-1",
      workspaceHandle: String(snapshot.workspace_handle),
      expectedWorkspaceRevision: Number(snapshot.workspace_revision),
      expectedTimelineRevision: Number(snapshot.timeline_revision),
      expectedTimelineFingerprint: String(snapshot.timeline_fingerprint),
    } as const;
    expect(() =>
      encodeTimelineTransaction({
        ...base,
        commands: [
          {
            kind: "select_clips",
            payload: { clip_ids: [], hidden_snapshot: {} },
          },
        ],
      }),
    ).toThrow(/payload.*closed/);
    expect(() =>
      encodeTimelineTransaction({
        ...base,
        commands: [{ kind: "set_gain", payload: {} }],
      }),
    ).toThrow(/audio_editing_deferred/);
  });

  it("rejects invalid command identifiers and open nested value objects", () => {
    const snapshot = compositionSnapshotWire();
    const base = {
      requestId: "req-history-shape",
      transactionId: "tx-history-shape",
      workspaceHandle: String(snapshot.workspace_handle),
      expectedWorkspaceRevision: Number(snapshot.workspace_revision),
      expectedTimelineRevision: Number(snapshot.timeline_revision),
      expectedTimelineFingerprint: String(snapshot.timeline_fingerprint),
    } as const;
    expect(() =>
      encodeTimelineTransaction({
        ...base,
        commands: [{ kind: "remove_clip", payload: { clip_id: "bad handle" } }],
      }),
    ).toThrow(/clip_id/);
    expect(() =>
      encodeTimelineTransaction({
        ...base,
        commands: [
          {
            kind: "set_visual_transform",
            payload: {
              clip_id: "clip-main",
              transform: {
                anchor_x_bp: 5000,
                anchor_y_bp: 5000,
                position_x_bp: 0,
                position_y_bp: 0,
                scale_x_bp: 10000,
                scale_y_bp: 10000,
                rotation_mdeg: 0,
                unexpected: 1,
              },
            },
          },
        ],
      }),
    ).toThrow(/transform.*closed/);
  });

  it("rejects inserted clip invariants and text controls owned by M25-10", () => {
    const snapshot = compositionSnapshotWire();
    const base = {
      requestId: "req-history-invariants",
      transactionId: "tx-history-invariants",
      workspaceHandle: String(snapshot.workspace_handle),
      expectedWorkspaceRevision: Number(snapshot.workspace_revision),
      expectedTimelineRevision: Number(snapshot.timeline_revision),
      expectedTimelineFingerprint: String(snapshot.timeline_fingerprint),
    } as const;
    const clip = JSON.parse(
      JSON.stringify((snapshot.clips as Record<string, unknown>[])[1]),
    ) as Record<string, unknown>;
    clip.clip_id = "clip-new-overlay";
    clip.duration_frames = 3;
    clip.transition = {
      kind: "cross_dissolve_v1",
      duration_frames: 4,
    };
    expect(() =>
      encodeTimelineTransaction({
        ...base,
        commands: [{ kind: "insert_asset_clip", payload: { clip } }],
      }),
    ).toThrow(/transition.*duration/);
    expect(() =>
      encodeTimelineTransaction({
        ...base,
        commands: [
          {
            kind: "set_text_content",
            payload: { clip_id: "clip-title", content: "line one\rline two" },
          },
        ],
      }),
    ).toThrow(/content.*bounds/);
  });

  it("accepts one closed representative payload for every exact command row", () => {
    const snapshot = compositionSnapshotWire();
    const clips = snapshot.clips as Record<string, unknown>[];
    const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
    const assetClip = clone(clips[2] as Record<string, unknown>);
    assetClip.clip_id = "clip-new-asset";
    const rangeClip = clone(assetClip);
    rangeClip.clip_id = "clip-new-range";
    const overwriteClip = clone(assetClip);
    overwriteClip.clip_id = "clip-new-overwrite";
    const titleClip = clone(clips[3] as Record<string, unknown>);
    titleClip.clip_id = "clip-new-title";
    const title = titleClip.text as Record<string, unknown>;
    const { content: _content, ...titleStyle } = title;
    const cursor = `h3.context.timeline_history_cursor.v1:1:${"a".repeat(64)}`;
    const validPayloads = {
      create_track: {
        track_id: "track-new",
        kind: "image_overlay",
        order: 1,
      },
      remove_track: { track_id: "track-image" },
      reorder_track: { track_id: "track-image", order: 1 },
      set_track_enabled: { track_id: "track-image", enabled: true },
      set_track_locked: { track_id: "track-image", locked: false },
      insert_asset_clip: { clip: assetClip },
      insert_title_clip: { clip: titleClip },
      replace_clip_asset: {
        clip_id: "clip-image",
        asset_id: "img-overlay",
        source_start_frame: 0,
      },
      remove_clip: { clip_id: "clip-image" },
      move_clip: {
        clip_id: "clip-image",
        delta_frames: 0,
        target_track_id: "track-image",
      },
      move_group: {
        clip_ids: ["clip-image"],
        delta_frames: 1,
        target_track_ids: ["track-image"],
      },
      trim_clip: { clip_id: "clip-main", edge: "end", delta_frames: -1 },
      split_clip: {
        clip_id: "clip-main",
        at_offset_frames: 24,
        right_clip_id: "clip-main-right",
      },
      merge_clips: {
        left_clip_id: "clip-left",
        right_clip_id: "clip-right",
      },
      insert_range: { clip: rangeClip, scope_track_ids: ["track-image"] },
      overwrite_range: {
        clip: overwriteClip,
        start_frame: 0,
        duration_frames: 48,
        scope_track_ids: ["track-image"],
        remainder_ids: {},
      },
      ripple_delete: {
        start_frame: 0,
        duration_frames: 1,
        scope_track_ids: ["track-image"],
        remainder_ids: {},
      },
      ripple_trim: {
        clip_id: "clip-main",
        edge: "end",
        delta_frames: -1,
        scope_track_ids: ["track-primary"],
      },
      roll_edit: {
        left_clip_id: "clip-left",
        right_clip_id: "clip-right",
        delta_frames: 1,
      },
      slip_clip: { clip_id: "clip-main", delta_frames: 1 },
      slide_clip: {
        clip_id: "clip-middle",
        left_clip_id: "clip-left",
        right_clip_id: "clip-right",
        delta_frames: 1,
      },
      set_clip_enabled: { clip_id: "clip-main", enabled: true },
      set_visual_transform: {
        clip_id: "clip-main",
        transform: clone((clips[0] as Record<string, unknown>).transform),
      },
      set_crop: {
        clip_id: "clip-main",
        crop: clone((clips[0] as Record<string, unknown>).crop),
      },
      set_opacity_blend: {
        clip_id: "clip-main",
        opacity_bp: 10_000,
        blend: "normal",
      },
      set_text_content: { clip_id: "clip-title", content: "New title" },
      set_text_style: { clip_id: "clip-title", style: titleStyle },
      set_transition: {
        clip_id: "clip-video-overlay",
        transition: clone((clips[1] as Record<string, unknown>).transition),
      },
      set_effect: {
        clip_id: "clip-video-overlay",
        effect: clone((clips[1] as Record<string, unknown>).effect),
      },
      set_clip_audio: {
        clip_id: "clip-main",
        gain_mb: -600,
        muted: false,
        fade_in_frames: 12,
        fade_out_frames: 12,
      },
      select_clips: { clip_ids: ["clip-main"] },
      undo: { history_cursor: cursor },
      redo: { history_cursor: cursor },
      rebase_transaction: {
        base_timeline_fingerprint: snapshot.timeline_fingerprint,
        commands: [
          { kind: "select_clips", payload: { clip_ids: ["clip-main"] } },
        ],
      },
    } satisfies Record<
      (typeof NLE_OPERATION_IDS)[number],
      Record<string, unknown>
    >;
    expect(Object.keys(validPayloads)).toEqual([...NLE_OPERATION_IDS]);
    for (const [index, kind] of NLE_OPERATION_IDS.entries()) {
      const transaction = encodeTimelineTransaction({
        requestId: `req-valid-${index}`,
        transactionId: `tx-valid-${index}`,
        workspaceHandle: String(snapshot.workspace_handle),
        expectedWorkspaceRevision: Number(snapshot.workspace_revision),
        expectedTimelineRevision: Number(snapshot.timeline_revision),
        expectedTimelineFingerprint: String(snapshot.timeline_fingerprint),
        commands: [{ kind, payload: validPayloads[kind] }],
      });
      expect(transaction.commands[0]?.kind).toBe(kind);
    }
  });

  it("uses backend-identical ASCII ordering and NFC normalization for canonical commands", () => {
    const snapshot = compositionSnapshotWire();
    const transaction = encodeTimelineTransaction({
      requestId: "req-history-order",
      transactionId: "tx-history-order",
      workspaceHandle: String(snapshot.workspace_handle),
      expectedWorkspaceRevision: Number(snapshot.workspace_revision),
      expectedTimelineRevision: Number(snapshot.timeline_revision),
      expectedTimelineFingerprint: String(snapshot.timeline_fingerprint),
      commands: [
        {
          kind: "move_group",
          payload: {
            clip_ids: ["clip-a", "clip-Z"],
            delta_frames: 1,
            target_track_ids: ["track-a", "track-Z"],
          },
        },
        {
          kind: "set_text_content",
          payload: { clip_id: "clip-title", content: "e\u0301" },
        },
      ],
    });
    expect(transaction.commands[0]?.payload).toMatchObject({
      clip_ids: ["clip-Z", "clip-a"],
      target_track_ids: ["track-Z", "track-a"],
    });
    expect(transaction.commands[1]?.payload.content).toBe("é");
  });

  it("mirrors backend selection sorting and duplicate canonicalization", () => {
    const snapshot = compositionSnapshotWire();
    const transaction = encodeTimelineTransaction({
      requestId: "req-history-selection-canonical",
      transactionId: "tx-history-selection-canonical",
      workspaceHandle: String(snapshot.workspace_handle),
      expectedWorkspaceRevision: Number(snapshot.workspace_revision),
      expectedTimelineRevision: Number(snapshot.timeline_revision),
      expectedTimelineFingerprint: String(snapshot.timeline_fingerprint),
      commands: [
        {
          kind: "select_clips",
          payload: {
            clip_ids: ["clip-main", "clip-Z", "clip-main", "clip-a"],
          },
        },
      ],
    });

    expect(transaction.commands[0]?.payload.clip_ids).toEqual([
      "clip-Z",
      "clip-a",
      "clip-main",
    ]);
  });
});

describe("M25-11 accepted history response codec", () => {
  it("decodes a complete receipt through the existing composition snapshot authority", () => {
    const receipt = decodeTimelineReceipt(receiptWire());
    expect(receipt.schema).toBe(TIMELINE_RECEIPT_SCHEMA);
    expect(receipt.workspaceHandle).toBe("workspace-fixture");
    expect(receipt.afterWorkspaceRevision).toBe(7);
    expect(receipt.snapshot.publicFingerprint).toBe(
      compositionFixture.snapshot.public_fingerprint,
    );
    expect(receipt.selection).toEqual(["clip-main"]);
    expect(Object.isFrozen(receipt.snapshot)).toBe(true);
  });

  it("decodes read/conflict projections and keeps rejection closed", () => {
    const read = decodeTimelineHistoryProjection(historyProjectionWire());
    expect(read.schema).toBe(TIMELINE_HISTORY_PROJECTION_SCHEMA);
    expect(read.rejection).toBeNull();
    expect(read.undoCursor).toMatch(/^h3\.context\.timeline_history_cursor/);
    expect(read.redoCursor).toBeNull();
    const conflict = decodeTimelineHistoryProjection(
      historyProjectionWire({
        rejection: { code: "stale_workspace_revision" },
      }),
    );
    expect(conflict.rejection?.code).toBe("stale_workspace_revision");
    expect(() =>
      decodeTimelineHistoryProjection(
        historyProjectionWire({
          rejection: { code: "stale_workspace_revision", detail: "private" },
        }),
      ),
    ).toThrow(/rejection.*closed/);
  });

  it("refuses a V2 projection with the legacy V1 member layout", () => {
    expect(() =>
      decodeTimelineHistoryProjection({
        ...historyProjectionWire(),
        schema: "h3.context.timeline_history_projection.v2",
      }),
    ).toThrow(/timeline history projection schema is unsupported/);
  });

  it("rejects open receipts, cross-workspace snapshots and inconsistent after CAS", () => {
    expect(() => decodeTimelineReceipt(receiptWire({ extra: true }))).toThrow(
      /receipt.*closed/,
    );
    expect(() =>
      decodeTimelineHistoryProjection(
        historyProjectionWire({ workspace_handle: "another-workspace" }),
      ),
    ).toThrow(/cross-workspace/);
    expect(() =>
      decodeTimelineReceipt(receiptWire({ after_workspace_revision: 8 })),
    ).toThrow(/after workspace revision/);
    expect(() =>
      decodeTimelineReceipt(
        receiptWire({
          commands: [{ kind: "set_gain", payload: {} }],
        }),
      ),
    ).toThrow(/audio_editing_deferred/);
  });
});
