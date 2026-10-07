import { describe, expect, it } from "vitest";

import type { TimelineCommandWire } from "../src/contracts/authoringWorkbenchCodec";
import type {
  CompositionClip,
  PublicCompositionSnapshot,
} from "../src/contracts/compositionCodec";
import {
  decideTimelineTool,
  eligibleRollCuts,
  resolveTimelineShortcut,
  toolPlayheadFrame,
  type TimelineToolContext,
} from "../src/components/nle/nleTimelineTools";
import {
  SMOKE_SHAPE,
  VIRTUALIZED_SHAPE,
  snapshotFixture,
} from "./support/nleWorkspaceFixture";

const cursor = `h3.context.timeline_history_cursor.v1:11:${"c".repeat(64)}`;

function withClips(
  snapshot: PublicCompositionSnapshot,
  clips: readonly CompositionClip[],
  lockedTrackIds: readonly string[] = [],
): PublicCompositionSnapshot {
  return {
    ...snapshot,
    clips,
    tracks: snapshot.tracks.map((track) => ({
      ...track,
      locked: lockedTrackIds.includes(track.trackId),
    })),
  };
}

function context(
  overrides: Partial<TimelineToolContext> = {},
): TimelineToolContext {
  const snapshot = snapshotFixture(SMOKE_SHAPE);
  const first = snapshot.clips[0]!;
  return {
    snapshot,
    selection: [first.clipId],
    authoringStatus: "ready",
    playheadFrame: first.startFrame + 12,
    rippleEnabled: false,
    undoCursor: cursor,
    redoCursor: null,
    rebaseAttempt: null,
    ...overrides,
  };
}

function payload(command: TimelineCommandWire) {
  return command.payload as Record<string, unknown>;
}

describe("M25-47 timeline tool decisions", () => {
  it("uses the latest ruler request until it settles, then falls back to the monitor frame", () => {
    expect(
      toolPlayheadFrame({
        requestedFrame: 41,
        monitorFrame: 12,
        transportAvailable: true,
      }),
    ).toBe(41);
    expect(
      toolPlayheadFrame({
        requestedFrame: null,
        monitorFrame: 12,
        transportAvailable: true,
      }),
    ).toBe(12);
    expect(
      toolPlayheadFrame({
        requestedFrame: 41,
        monitorFrame: 12,
        transportAvailable: false,
      }),
    ).toBeNull();
  });

  it("builds split and playhead trims only for one editable clip at a strict interior frame", () => {
    const base = context();
    const clip = base.snapshot.clips[0]!;
    const split = decideTimelineTool({ kind: "split" }, base);
    expect(split.enabled).toBe(true);
    expect(split.commands).toHaveLength(1);
    expect(split.commands[0]?.kind).toBe("split_clip");
    expect(payload(split.commands[0]!)).toEqual({
      clip_id: clip.clipId,
      at_offset_frames: 12,
      right_clip_id: `clip-r${base.snapshot.timelineRevision}-1`,
    });

    expect(
      payload(decideTimelineTool({ kind: "trim_start" }, base).commands[0]!),
    ).toEqual({ clip_id: clip.clipId, edge: "start", delta_frames: 12 });
    expect(
      payload(decideTimelineTool({ kind: "trim_end" }, base).commands[0]!),
    ).toEqual({
      clip_id: clip.clipId,
      edge: "end",
      delta_frames: 12 - clip.durationFrames,
    });
    for (const kind of ["split", "trim_start"] as const)
      expect(
        decideTimelineTool(kind === "split" ? { kind } : { kind }, {
          ...base,
          playheadFrame: clip.startFrame + 1,
        }),
      ).toMatchObject({
        enabled: false,
        reason: "source_range_unavailable",
        commands: [],
      });

    for (const playheadFrame of [
      clip.startFrame,
      clip.startFrame + clip.durationFrames,
    ])
      expect(
        decideTimelineTool({ kind: "split" }, { ...base, playheadFrame }),
      ).toMatchObject({
        enabled: false,
        reason: "playhead_outside_clip",
        commands: [],
      });
    expect(
      decideTimelineTool(
        { kind: "split" },
        { ...base, selection: [clip.clipId, base.snapshot.clips[1]!.clipId] },
      ),
    ).toMatchObject({ enabled: false, reason: "single_selection_required" });
    expect(
      decideTimelineTool(
        { kind: "split" },
        { ...base, snapshot: snapshotFixture(VIRTUALIZED_SHAPE) },
      ),
    ).toMatchObject({ enabled: false, reason: "clip_capacity" });
  });

  it("switches both playhead trims to one-track ripple commands without changing their deltas", () => {
    const base = context({ rippleEnabled: true });
    const clip = base.snapshot.clips[0]!;
    for (const kind of ["trim_start", "trim_end"] as const) {
      const decision = decideTimelineTool({ kind }, base);
      expect(decision.commands[0]?.kind).toBe("ripple_trim");
      expect(payload(decision.commands[0]!)).toEqual({
        clip_id: clip.clipId,
        edge: kind === "trim_start" ? "start" : "end",
        delta_frames: kind === "trim_start" ? 12 : 12 - clip.durationFrames,
        scope_track_ids: [clip.trackId],
      });
    }
  });

  it("orders ordinary deletes deterministically and refuses the whole transaction on stale, locked or oversized selection", () => {
    const base = context();
    const chosen = [
      base.snapshot.clips[5]!,
      base.snapshot.clips[0]!,
      base.snapshot.clips[4]!,
    ];
    const decision = decideTimelineTool(
      { kind: "delete" },
      { ...base, selection: chosen.map((clip) => clip.clipId) },
    );
    expect(
      decision.commands.map((command) => payload(command).clip_id),
    ).toEqual(
      [...chosen]
        .sort(
          (left, right) =>
            left.trackId.localeCompare(right.trackId) ||
            left.startFrame - right.startFrame ||
            left.clipId.localeCompare(right.clipId),
        )
        .map((clip) => clip.clipId),
    );

    expect(
      decideTimelineTool(
        { kind: "delete" },
        { ...base, selection: [...base.selection, "missing"] },
      ),
    ).toMatchObject({
      enabled: false,
      reason: "stale_selection",
      commands: [],
    });
    expect(
      decideTimelineTool(
        { kind: "delete" },
        {
          ...base,
          snapshot: withClips(base.snapshot, base.snapshot.clips, [
            base.snapshot.clips[0]!.trackId,
          ]),
        },
      ),
    ).toMatchObject({ enabled: false, reason: "locked_track", commands: [] });
    expect(
      decideTimelineTool(
        { kind: "delete" },
        {
          ...base,
          selection: Array.from(
            { length: 33 },
            (_, index) => base.snapshot.clips[index]!.clipId,
          ),
        },
      ),
    ).toMatchObject({ enabled: false, reason: "command_limit", commands: [] });
  });

  it("uses exactly one selected clip and its track for ripple delete, including Shift+Delete override", () => {
    const base = context();
    const clip = base.snapshot.clips[0]!;
    const decision = decideTimelineTool(
      { kind: "delete", forceRipple: true },
      base,
    );
    expect(decision.commands).toHaveLength(1);
    expect(decision.commands[0]?.kind).toBe("ripple_delete");
    expect(payload(decision.commands[0]!)).toEqual({
      start_frame: clip.startFrame,
      duration_frames: clip.durationFrames,
      scope_track_ids: [clip.trackId],
      remainder_ids: {},
    });
    expect(
      decideTimelineTool(
        { kind: "delete", forceRipple: true },
        {
          ...base,
          selection: base.snapshot.clips.slice(0, 2).map((row) => row.clipId),
        },
      ),
    ).toMatchObject({ enabled: false, reason: "single_selection_required" });
  });

  it("uses current history cursors and only the exact eligible rejected attempt for rebase", () => {
    const base = context();
    expect(decideTimelineTool({ kind: "undo" }, base).commands[0]).toEqual({
      kind: "undo",
      payload: { history_cursor: cursor },
    });
    expect(decideTimelineTool({ kind: "redo" }, base)).toMatchObject({
      enabled: false,
      reason: "history_unavailable",
    });
    const attempt = {
      baseTimelineFingerprint: "sha256:" + "d".repeat(64),
      commands: [
        {
          kind: "set_clip_enabled",
          payload: { clip_id: "clip-0", enabled: false },
        },
      ] as readonly TimelineCommandWire[],
    };
    expect(
      decideTimelineTool(
        { kind: "rebase" },
        { ...base, authoringStatus: "conflict", rebaseAttempt: attempt },
      ).commands[0],
    ).toEqual({
      kind: "rebase_transaction",
      payload: {
        base_timeline_fingerprint: attempt.baseTimelineFingerprint,
        commands: attempt.commands,
      },
    });
    expect(
      decideTimelineTool(
        { kind: "rebase" },
        {
          ...base,
          authoringStatus: "conflict",
          rebaseAttempt: {
            ...attempt,
            commands: [
              { kind: "move_clip", payload: {} },
            ] as readonly TimelineCommandWire[],
          },
        },
      ),
    ).toMatchObject({ enabled: false, reason: "rebase_unavailable" });
  });

  it("finds only editable same-track adjacent cuts and refuses unrepresentable source shifts", () => {
    const base = context();
    const original = base.snapshot.clips[0]!;
    const left = {
      ...original,
      clipId: "left",
      startFrame: 10,
      durationFrames: 10,
    };
    const right = {
      ...original,
      clipId: "right",
      startFrame: 20,
      durationFrames: 8,
    };
    const gap = {
      ...original,
      clipId: "gap",
      startFrame: 31,
      durationFrames: 5,
    };
    const snapshot = withClips(base.snapshot, [right, gap, left]);
    expect(eligibleRollCuts(snapshot, original.trackId)).toEqual([
      { leftClipId: "left", rightClipId: "right", frame: 20 },
    ]);
    expect(
      decideTimelineTool(
        {
          kind: "roll",
          leftClipId: "left",
          rightClipId: "right",
          deltaFrames: 2,
        },
        { ...base, snapshot },
      ),
    ).toMatchObject({
      enabled: false,
      reason: "source_range_unavailable",
      commands: [],
    });
    const admittedRight = { ...right, durationFrames: 24 };
    const admittedSnapshot = withClips(base.snapshot, [admittedRight, left]);
    expect(
      decideTimelineTool(
        {
          kind: "roll",
          leftClipId: "left",
          rightClipId: "right",
          deltaFrames: 12,
        },
        { ...base, snapshot: admittedSnapshot },
      ).commands[0],
    ).toEqual({
      kind: "roll_edit",
      payload: {
        left_clip_id: "left",
        right_clip_id: "right",
        delta_frames: 12,
      },
    });
    expect(
      decideTimelineTool(
        {
          kind: "roll",
          leftClipId: "left",
          rightClipId: "right",
          deltaFrames: 8,
        },
        { ...base, snapshot },
      ),
    ).toMatchObject({ enabled: false, reason: "empty_roll_side" });
  });
});

describe("M25-47 overlay shortcut resolver", () => {
  const key = (overrides: Record<string, unknown>) => ({
    key: "",
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    altKey: false,
    repeat: false,
    isComposing: false,
    defaultPrevented: false,
    target: document.createElement("div"),
    ...overrides,
  });

  it("maps the complete owned shortcut set and marks discrete repeats as contained but inert", () => {
    expect(resolveTimelineShortcut(key({ key: "b", ctrlKey: true }))).toEqual({
      action: "split",
      invoke: true,
    });
    expect(
      resolveTimelineShortcut(key({ key: "Delete", shiftKey: true })),
    ).toEqual({
      action: "ripple_delete",
      invoke: true,
    });
    expect(
      resolveTimelineShortcut(key({ key: "z", ctrlKey: true, shiftKey: true })),
    ).toEqual({
      action: "redo",
      invoke: true,
    });
    expect(
      resolveTimelineShortcut(key({ key: "=", ctrlKey: true })),
    ).toMatchObject({
      action: "zoom_in",
    });
    expect(resolveTimelineShortcut(key({ key: "+" }))).toMatchObject({
      action: "zoom_in",
    });
    expect(resolveTimelineShortcut(key({ key: "-" }))).toMatchObject({
      action: "zoom_out",
    });
    expect(resolveTimelineShortcut(key({ key: " ", repeat: true }))).toEqual({
      action: "toggle_play",
      invoke: true,
    });
    expect(resolveTimelineShortcut(key({ key: "q", repeat: true }))).toEqual({
      action: "trim_start",
      invoke: false,
    });
  });

  it("gives editable/native children, IME, handled events and AltGraph combinations first refusal", () => {
    const input = document.createElement("input");
    const button = document.createElement("button");
    for (const event of [
      key({ key: "q", target: input }),
      key({ key: " ", target: button }),
      key({ key: "q", isComposing: true }),
      key({ key: "q", defaultPrevented: true }),
      key({ key: "b", ctrlKey: true, altKey: true }),
      key({ key: "x" }),
    ])
      expect(resolveTimelineShortcut(event)).toBeNull();
  });

  const buttonKeys = [
    { key: "Delete", action: "delete" },
    { key: "Delete", shiftKey: true, action: "ripple_delete" },
    { key: "b", ctrlKey: true, action: "split" },
    { key: "q", action: "trim_start" },
    { key: "w", action: "trim_end" },
    { key: "z", ctrlKey: true, action: "undo" },
    { key: "z", metaKey: true, shiftKey: true, action: "redo" },
    { key: "a", ctrlKey: true, action: "select_all" },
    { key: "=", action: "zoom_in" },
    { key: "-", action: "zoom_out" },
    { key: "z", shiftKey: true, action: "zoom_fit" },
    { key: ",", action: "step_back" },
    { key: ".", action: "step_forward" },
  ];

  it.each(buttonKeys)(
    "resolves $action from a timeline button and its icon",
    ({ action, ...event }) => {
      const region = document.createElement("section");
      region.setAttribute("data-h3-nle-region", "timeline");
      const button = document.createElement("button");
      const icon = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "svg",
      );
      region.append(button);
      button.append(icon);
      for (const target of [button, icon]) {
        expect(resolveTimelineShortcut(key({ ...event, target }))).toEqual({
          action,
          invoke: true,
        });
        const discrete = [
          "delete",
          "ripple_delete",
          "split",
          "trim_start",
          "trim_end",
          "undo",
          "redo",
          "select_all",
        ].includes(action);
        expect(
          resolveTimelineShortcut(key({ ...event, target, repeat: true })),
        ).toEqual({ action, invoke: !discrete });
      }
    },
  );

  it("preserves native button keys and every non-timeline or explicit child owner", () => {
    const region = document.createElement("section");
    region.setAttribute("data-h3-nle-region", "timeline");
    const button = document.createElement("button");
    region.append(button);
    for (const nativeKey of [
      " ",
      "Enter",
      "Escape",
      "ArrowLeft",
      "ArrowRight",
      "Home",
      "End",
    ])
      expect(
        resolveTimelineShortcut(key({ key: nativeKey, target: button })),
      ).toBeNull();
    for (const owner of [
      "input",
      "textarea",
      "select",
      '[contenteditable="true"]',
      '[role="slider"]',
      '[role="spinbutton"]',
      '[role="menu"]',
      '[role="menuitem"]',
      "[data-h3-nle-shortcut-owner]",
    ]) {
      const child = document.createElement(
        owner.startsWith("[") ? "div" : owner,
      );
      if (owner.startsWith("[")) {
        const [name, value] = owner.slice(1, -1).split("=");
        child.setAttribute(name!, value?.replaceAll('"', "") ?? "");
      }
      region.replaceChildren(child);
      child.append(button);
      for (const { action: _action, ...event } of buttonKeys)
        expect(
          resolveTimelineShortcut(key({ ...event, target: button })),
        ).toBeNull();
    }
    region.removeChild(region.firstChild!);
    const outside = document.createElement("button");
    for (const { action: _action, ...event } of buttonKeys)
      expect(
        resolveTimelineShortcut(key({ ...event, target: outside })),
      ).toBeNull();
  });
});
