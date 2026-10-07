import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthoringViewState } from "../src/state/authoringViewState";
import {
  NleTimeline,
  type NleTimelineShortcutHandler,
  type NleTimelineTransportChannel,
} from "../src/components/nle/NleTimeline";
import { NleWorkspace } from "../src/components/nle/NleWorkspace";
import type { PublicCompositionSnapshot } from "../src/contracts/compositionCodec";
import { SMOKE_SHAPE, authoringReady } from "./support/nleWorkspaceFixture";
import {
  availableDisposition,
  bindingFixture,
  expandedState,
  unavailableDisposition,
} from "./support/nleWorkspaceBinding";

type Ready = AuthoringViewState & { status: "ready" };
type TimelineSpy = { mock: { calls: unknown[][] } };

function lastCommand(spy: TimelineSpy) {
  const intent = spy.mock.calls.at(-1)?.[0] as
    | { commands: { kind: string; payload: Record<string, unknown> }[] }
    | undefined;
  return intent?.commands[0];
}

function ready(selection = ["clip-0"]): Ready {
  return authoringReady(SMOKE_SHAPE, {
    selection,
    redo_cursor: `h3.context.timeline_history_cursor.v1:12:${"b".repeat(64)}`,
  }) as Ready;
}

function adjacentSnapshot(authoring: Ready): PublicCompositionSnapshot {
  const snapshot = authoring.timelineHistory!.snapshot;
  const left = snapshot.clips.find((clip) => clip.clipId === "clip-0")!;
  return {
    ...snapshot,
    clips: snapshot.clips.map((clip) =>
      clip.clipId === "clip-4"
        ? { ...clip, startFrame: left.startFrame + left.durationFrames }
        : clip,
    ),
  };
}

function timelineSubject(
  snapshot?: PublicCompositionSnapshot,
  frameOffset = 12,
  contentEndExclusive?: number,
) {
  const authoring = ready();
  const current = snapshot ?? authoring.timelineHistory!.snapshot;
  const timeline = vi.fn(async () => undefined);
  const seek = vi.fn();
  const channel: NleTimelineTransportChannel = {
    snapshot: () => ({
      frame: current.clips[0]!.startFrame + frameOffset,
      request: null,
      settledRequestGeneration: 0,
      transportAvailable: true,
    }),
    subscribe: () => () => undefined,
  };
  let shortcut: NleTimelineShortcutHandler | null = null;
  const view = render(
    <div data-testid="overlay" onKeyDown={(event) => shortcut?.(event)}>
      <NleTimeline
        locale="en"
        snapshot={current}
        selection={["clip-0"]}
        authoring={{
          ...authoring,
          timelineHistory: { ...authoring.timelineHistory!, snapshot: current },
        }}
        gridFrames={1}
        editCapacityFrames={3_600}
        contentEndExclusive={contentEndExclusive}
        playheadFrame={current.clips[0]!.startFrame + frameOffset}
        transportChannel={channel}
        highlightedAssetIds={[]}
        onIntent={timeline}
        onSeek={seek}
        onEdgeGestureActive={() => undefined}
        bindShortcuts={(handler) => {
          shortcut = handler;
          return () => {
            if (shortcut === handler) shortcut = null;
          };
        }}
      />
    </div>,
  );
  return { view, timeline, seek };
}

beforeEach(() => {
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
  Element.prototype.hasPointerCapture = vi.fn(() => true);
});

afterEach(cleanup);

describe("M25-47 integrated timeline tools", () => {
  it("M25-55 keeps the logical cursor and frame navigation when preview media is unavailable", () => {
    const binding = bindingFixture({
      authoring: ready(),
      runtime: unavailableDisposition(),
    });
    const view = render(
      <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />,
    );
    const ruler = view.getByRole("slider", { name: "Playhead" });
    const next = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-alternative="seek.next_frame"]',
    )!;
    expect(ruler.getAttribute("aria-disabled")).toBe("false");
    expect((next as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(next);
    expect(ruler.getAttribute("aria-valuenow")).toBe("1");
  });

  it("M25-55 clamps the logical cursor once when accepted content shrinks", () => {
    const element = (authoring: Ready) => (
      <NleWorkspace
        binding={bindingFixture({
          authoring,
          runtime: unavailableDisposition(),
        })}
        onEdgeGestureActive={() => undefined}
      />
    );
    const view = render(element(ready()));
    const ruler = view.getByRole("slider", { name: "Playhead" });
    fireEvent.keyDown(ruler, { key: "End" });
    expect(ruler.getAttribute("aria-valuenow")).toBe("2879");

    const short = authoringReady(
      { ...SMOKE_SHAPE, name: "m25-55-short", durationSeconds: 2 },
      { selection: ["clip-0"] },
    ) as Ready;
    view.rerender(element(short));
    expect(ruler.getAttribute("aria-valuenow")).toBe("47");
  });

  it("bounds transport at content end while keeping the V2 edit capacity available", () => {
    const { view, seek } = timelineSubject(undefined, 12, 248);
    const ruler = view.getByRole("slider", { name: "Playhead" });

    expect(ruler.getAttribute("aria-valuemax")).toBe("247");
    fireEvent.keyDown(ruler, { key: "End" });
    expect(seek).toHaveBeenCalledWith(247);
  });

  it.each(["assets", "text"] as const)(
    "replaces a rejected attribute attempt when the %s bin issues a later nonrebasable command",
    (pane) => {
      const shape = { ...SMOKE_SHAPE, clipCount: 1 };
      const current = (revision: number, status: "ready" | "conflict") => {
        const value = authoringReady(shape, {
          selection: ["clip-0"],
          revision,
        }) as Ready;
        return {
          ...value,
          status,
          timelineHistory: {
            ...value.timelineHistory!,
            snapshot: {
              ...value.timelineHistory!.snapshot,
              timelineFingerprint: `sha256:${revision.toString(16).padStart(64, "0")}`,
            },
          },
        };
      };
      const state = expandedState({}, { width: 1280, height: 800 });
      const binding = bindingFixture({
        authoring: current(11, "ready"),
        runtime: availableDisposition(),
        state: { ...state, surface: { ...state.surface, pane } },
      });
      const timeline = binding.actions.timeline as unknown as TimelineSpy;
      const element = (revision: number, status: "ready" | "conflict") => (
        <NleWorkspace
          binding={{ ...binding, authoring: current(revision, status) }}
          onEdgeGestureActive={() => undefined}
        />
      );
      const view = render(element(11, "ready"));
      // M25-62 (B-M2562-03): the selected clip is narrower than the 96 px inline trigger rule, so
      // its menu opens by a context click on the body, as a mouse user does.
      fireEvent.contextMenu(
        view.container.querySelector(
          '.h3-nle-clip[data-selected="true"] [data-h3-nle-control="selection.set"]',
        )!,
      );
      fireEvent.click(
        view.container.querySelector('[data-h3-nle-control="clip.enabled"]')!,
      );
      expect(lastCommand(timeline)?.kind).toBe("set_clip_enabled");
      view.rerender(element(12, "conflict"));
      const rebase = () =>
        view.getByRole("button", {
          name: "Rebase rejected edit",
        });
      expect(rebase().getAttribute("aria-disabled")).not.toBe("true");

      const selector = pane === "assets" ? "range.overwrite" : "title.insert";
      // M25-63: range commands live in the card's menu, opened by a context click on the card.
      if (pane === "assets")
        fireEvent.contextMenu(
          view.container.querySelector(".h3-nle-media-primary")!,
        );
      const control = document.querySelector<HTMLButtonElement>(
        `[data-h3-nle-control="${selector}"]`,
      )!;
      expect(control.disabled).toBe(false);
      fireEvent.click(control);
      expect(lastCommand(timeline)?.kind).toBe(
        pane === "assets" ? "overwrite_range" : "insert_title_clip",
      );
      view.rerender(element(13, "conflict"));
      expect(rebase().getAttribute("aria-disabled")).toBe("true");
      const before = timeline.mock.calls.length;
      fireEvent.click(rebase());
      expect(timeline.mock.calls).toHaveLength(before);
    },
  );

  it("routes toolbar tools through the one command seam and changes trim/delete with local ripple mode", () => {
    const { view, timeline } = timelineSubject();
    fireEvent.click(view.getByRole("button", { name: "Split at playhead" }));
    expect(lastCommand(timeline)?.kind).toBe("split_clip");

    fireEvent.click(view.getByRole("button", { name: "Main-track ripple" }));
    fireEvent.click(
      view.getByRole("button", { name: "Trim start to playhead" }),
    );
    expect(lastCommand(timeline)?.kind).toBe("ripple_trim");
    fireEvent.click(view.getByRole("button", { name: "Ripple delete" }));
    expect(lastCommand(timeline)?.kind).toBe("ripple_delete");
  });

  it("uses the synchronous ruler request when split follows a seek before monitor settlement", () => {
    const { view, timeline } = timelineSubject(undefined, 8);
    const ruler = view.getByRole("slider", { name: "Playhead" });
    for (let step = 0; step < 4; step += 1)
      fireEvent.keyDown(ruler, { key: "ArrowRight" });
    fireEvent.click(view.getByRole("button", { name: "Split at playhead" }));
    expect(lastCommand(timeline)).toMatchObject({
      kind: "split_clip",
      payload: { clip_id: "clip-0", at_offset_frames: 12 },
    });
  });

  it("captures ripple mode for an edge-grip commit and scopes the command to that track", () => {
    const { view, timeline } = timelineSubject();
    fireEvent.click(view.getByRole("button", { name: "Main-track ripple" }));
    const grip = view.container.querySelector<HTMLButtonElement>(
      '[data-h3-nle-trim-clip="clip-0"] [data-h3-nle-trim-edge="end"]:not([hidden])',
    )!;
    expect(grip).not.toBeNull();
    fireEvent.keyDown(grip, { key: "ArrowLeft" });
    fireEvent.keyDown(grip, { key: "Enter" });
    expect(lastCommand(timeline)).toMatchObject({
      kind: "ripple_trim",
      payload: {
        clip_id: "clip-0",
        edge: "end",
        scope_track_ids: ["track-0"],
      },
    });
  });

  it("maps Q and W through the bound overlay handler to the same playhead trim seam", () => {
    const { view, timeline } = timelineSubject();
    const overlay = view.getByTestId("overlay");
    fireEvent.keyDown(overlay, { key: "q" });
    expect(lastCommand(timeline)).toMatchObject({
      kind: "trim_clip",
      payload: { edge: "start", delta_frames: 12 },
    });
    fireEvent.keyDown(overlay, { key: "w" });
    expect(lastCommand(timeline)).toMatchObject({
      kind: "trim_clip",
      payload: { edge: "end" },
    });
  });

  it("contains overlay shortcuts once, ignores repeats and lets editable controls keep their keys", () => {
    const binding = bindingFixture({
      authoring: ready(),
      runtime: availableDisposition(),
      state: expandedState({}, { width: 1280, height: 800 }),
    });
    const view = render(
      <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />,
    );
    const timeline = binding.actions.timeline as unknown as TimelineSpy;
    const stage = view.container.querySelector<HTMLElement>(".h3-nle-stage")!;
    fireEvent.keyDown(stage, { key: "z", ctrlKey: true });
    expect(lastCommand(timeline)?.kind).toBe("undo");
    const count = timeline.mock.calls.length;
    fireEvent.keyDown(stage, { key: "z", ctrlKey: true, repeat: true });
    expect(timeline).toHaveBeenCalledTimes(count);
    const input = view.container.querySelector<HTMLInputElement>(
      '.h3-nle-inspector input[type="number"]',
    )!;
    fireEvent.keyDown(input, { key: "Delete" });
    expect(timeline).toHaveBeenCalledTimes(count);
    fireEvent.keyDown(stage, { key: "Delete" });
    expect(lastCommand(timeline)?.kind).toBe("remove_clip");
  });

  it("commits the same roll command from pointer, focused-cut keys and the click stepper", () => {
    const authoring = ready();
    const { view, timeline } = timelineSubject(adjacentSnapshot(authoring));
    const cut = view.container.querySelector<HTMLButtonElement>(
      '.h3-nle-roll-cut[data-h3-nle-control="boundary.roll"]:not([hidden])',
    )!;
    expect(cut).not.toBeNull();

    fireEvent.pointerDown(cut, {
      pointerId: 1,
      button: 0,
      isPrimary: true,
      clientX: 100,
    });
    fireEvent.pointerMove(cut, { pointerId: 1, clientX: 112 });
    fireEvent.pointerUp(cut, { pointerId: 1, clientX: 112 });
    fireEvent.click(cut);
    expect(lastCommand(timeline)).toMatchObject({
      kind: "roll_edit",
      payload: { delta_frames: 12 },
    });

    for (let step = 0; step < 12; step += 1)
      fireEvent.keyDown(cut, { key: "ArrowRight" });
    fireEvent.keyDown(cut, { key: "Enter" });
    expect(lastCommand(timeline)).toMatchObject({
      kind: "roll_edit",
      payload: { delta_frames: 12 },
    });

    fireEvent.click(cut);
    for (let step = 0; step < 12; step += 1)
      fireEvent.click(view.getByRole("button", { name: "Roll edit +1" }));
    fireEvent.click(view.getByRole("button", { name: "Apply" }));
    expect(lastCommand(timeline)).toMatchObject({
      kind: "roll_edit",
      payload: { delta_frames: 12 },
    });
  });

  const shortcutTargets = [
    [
      "clip",
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    ],
    ["toolbar", '[data-h3-nle-control="transport.zoom_fit"]'],
    [
      "trim grip",
      '[data-h3-nle-trim-clip="clip-0"] [data-h3-nle-trim-edge="end"]:not([hidden])',
    ],
    ["roll handle", '[data-h3-nle-control="boundary.roll"]:not([hidden])'],
  ] as const;
  const editKeys = [
    { key: "q", kind: "trim_clip" },
    { key: "w", kind: "trim_clip" },
    { key: "b", ctrlKey: true, kind: "split_clip" },
    { key: "Delete", kind: "remove_clip" },
    { key: "Delete", shiftKey: true, kind: "ripple_delete" },
    { key: "z", ctrlKey: true, kind: "undo" },
    { key: "z", ctrlKey: true, shiftKey: true, kind: "redo" },
    { key: "a", ctrlKey: true, kind: "select_clips" },
  ];
  describe.each(shortcutTargets)("shortcuts from the %s", (_name, selector) => {
    it.each(editKeys)(
      "dispatches $kind once for $key and contains a repeat",
      ({ kind, ...event }) => {
        const { view, timeline } = timelineSubject(adjacentSnapshot(ready()));
        const target =
          view.container.querySelector<HTMLButtonElement>(selector)!;
        expect(target).not.toBeNull();
        const foreign = vi.fn();
        window.addEventListener("keydown", foreign);
        try {
          fireEvent.keyDown(target, event);
          expect(lastCommand(timeline)?.kind).toBe(kind);
          expect(timeline).toHaveBeenCalledTimes(1);
          fireEvent.keyDown(target, { ...event, repeat: true });
          expect(timeline).toHaveBeenCalledTimes(1);
          expect(foreign).not.toHaveBeenCalled();
        } finally {
          window.removeEventListener("keydown", foreign);
        }
      },
    );

    it("changes view scale and fit without a timeline transaction", () => {
      const { view, timeline } = timelineSubject(adjacentSnapshot(ready()));
      const target = view.container.querySelector<HTMLButtonElement>(selector)!;
      const region = view.container.querySelector<HTMLElement>(
        '[data-h3-nle-region="timeline"]',
      )!;
      const scale = () =>
        Number(region.getAttribute("data-h3-nle-pixels-per-frame"));
      const before = scale();
      fireEvent.keyDown(target, { key: "=" });
      expect(scale()).toBeGreaterThan(before);
      fireEvent.keyDown(target, { key: "-" });
      expect(scale()).toBe(before);
      fireEvent.keyDown(target, { key: "z", shiftKey: true });
      expect(scale()).toBeGreaterThan(0);
      expect(timeline).not.toHaveBeenCalled();
    });
  });

  it.each(["move", "trim", "roll"])(
    "keeps a %s keyboard draft exclusive while containing workspace keys",
    (draft) => {
      const { view, timeline } = timelineSubject(adjacentSnapshot(ready()));
      const selector =
        draft === "move"
          ? shortcutTargets[0][1]
          : draft === "trim"
            ? shortcutTargets[2][1]
            : shortcutTargets[3][1];
      const target = view.container.querySelector<HTMLButtonElement>(selector)!;
      fireEvent.keyDown(target, {
        key: draft === "move" ? "Enter" : "ArrowRight",
      });
      const region = view.container.querySelector<HTMLElement>(
        '[data-h3-nle-region="timeline"]',
      )!;
      const beforeScale = region.getAttribute("data-h3-nle-pixels-per-frame");
      const foreign = vi.fn();
      window.addEventListener("keydown", foreign);
      try {
        for (const { kind: _kind, ...event } of editKeys)
          fireEvent.keyDown(target, event);
        fireEvent.keyDown(target, { key: "=" });
        expect(timeline).not.toHaveBeenCalled();
        expect(region.getAttribute("data-h3-nle-pixels-per-frame")).toBe(
          beforeScale,
        );
        expect(foreign).not.toHaveBeenCalled();
      } finally {
        window.removeEventListener("keydown", foreign);
        fireEvent.keyDown(target, { key: "Escape" });
      }
    },
  );
});
