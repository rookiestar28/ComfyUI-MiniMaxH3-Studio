import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  NleClipMenu,
  NleTrackMenu,
  type TimelineMenuTarget,
} from "../src/components/nle/NleTimelineMenus";
import { SMOKE_SHAPE, snapshotFixture } from "./support/nleWorkspaceFixture";

afterEach(() => cleanup());

function target(
  id: string | null,
  button: HTMLButtonElement,
): TimelineMenuTarget {
  return { id, returnFocus: button, x: 12, y: 16 };
}

function firstCommand(onIntent: ReturnType<typeof vi.fn>) {
  return (
    onIntent.mock.calls.at(-1)?.[0] as {
      commands: readonly { kind: string; payload: Record<string, unknown> }[];
    }
  ).commands[0]!;
}

describe("M25-50 timeline context menus", () => {
  it("exposes track add/reorder/remove as one keyboard-roving menu and restores focus", async () => {
    const trigger = document.createElement("button");
    document.body.append(trigger);
    const onIntent = vi.fn(async () => undefined);
    const onClose = vi.fn();
    render(
      <NleTrackMenu
        locale="en"
        snapshot={snapshotFixture(SMOKE_SHAPE)}
        target={target("track-1", trigger)}
        busy={false}
        onIntent={onIntent}
        onClose={onClose}
      />,
    );
    const menu = screen.getByRole("menu", { name: "Track menu" });
    const add = screen.getByRole("menuitem", { name: "Add track" });
    expect(document.activeElement).toBe(add);
    fireEvent.change(screen.getByRole("spinbutton", { name: "Order" }), {
      target: { value: "2" },
    });
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(
      screen.getByRole("menuitem", { name: "Reorder track" }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Reorder track" }));
    expect(firstCommand(onIntent)).toEqual({
      kind: "reorder_track",
      payload: { track_id: "track-1", order: 2 },
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    await Promise.resolve();
    expect(document.activeElement).toBe(trigger);
  });

  it("keeps Add track reachable when no track is targeted", () => {
    const trigger = document.createElement("button");
    const onIntent = vi.fn(async () => undefined);
    render(
      <NleTrackMenu
        locale="en"
        snapshot={snapshotFixture(SMOKE_SHAPE)}
        target={target(null, trigger)}
        busy={false}
        onIntent={onIntent}
        onClose={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Add track" }));
    expect(firstCommand(onIntent).kind).toBe("create_track");
  });

  it("moves replace/slip/slide/merge/enabled to the clip menu with exact disabled states", () => {
    const trigger = document.createElement("button");
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    const base = snapshot.clips.find((clip) => clip.clipId === "clip-0")!;
    const right = snapshot.clips.find((clip) => clip.clipId === "clip-4")!;
    const left = snapshot.clips.find((clip) => clip.clipId === "clip-8")!;
    const adjacent = {
      ...snapshot,
      clips: snapshot.clips.map((clip) =>
        clip.clipId === right.clipId
          ? { ...clip, startFrame: base.startFrame + base.durationFrames }
          : clip.clipId === left.clipId
            ? {
                ...clip,
                startFrame: base.startFrame - clip.durationFrames,
              }
            : clip,
      ),
    };
    const onIntent = vi.fn(async () => undefined);
    render(
      <NleClipMenu
        locale="en"
        snapshot={adjacent}
        target={target(base.clipId, trigger)}
        busy={false}
        onIntent={onIntent}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByRole("menu", { name: "Clip menu" })).not.toBeNull();
    expect(
      (
        screen.getByRole("menuitem", {
          name: "Slide clip",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    expect(
      (
        screen.getByRole("menuitem", {
          name: "Merge clips",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    fireEvent.click(screen.getByRole("menuitem", { name: "Clip enabled" }));
    expect(firstCommand(onIntent)).toEqual({
      kind: "set_clip_enabled",
      payload: { clip_id: "clip-0", enabled: false },
    });
  });

  it.each([
    ["Trim start earlier by one frame", "start", -1],
    ["Trim start later by one frame", "start", 1],
    ["Trim end earlier by one frame", "end", -1],
    ["Trim end later by one frame", "end", 1],
  ] as const)(
    "provides a click-only %s operation",
    async (label, edge, delta) => {
      const trigger = document.createElement("button");
      const snapshot = snapshotFixture(SMOKE_SHAPE);
      const clip = snapshot.clips.find((member) => member.clipId === "clip-0")!;
      const onIntent = vi.fn(async () => undefined);
      render(
        <NleClipMenu
          locale="en"
          snapshot={snapshot}
          target={target(clip.clipId, trigger)}
          busy={false}
          rippleEnabled={false}
          onIntent={onIntent}
          onClose={() => undefined}
        />,
      );

      fireEvent.click(screen.getByRole("menuitem", { name: label }));

      expect(onIntent).toHaveBeenCalledTimes(1);
      expect(firstCommand(onIntent)).toEqual({
        kind: "trim_clip",
        payload: {
          clip_id: clip.clipId,
          edge,
          delta_frames: delta,
        },
      });
    },
  );

  it("routes click-only extension through ripple trim when the actual toggle is on", () => {
    const trigger = document.createElement("button");
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    const clip = snapshot.clips.find((member) => member.clipId === "clip-0")!;
    const onIntent = vi.fn(async () => undefined);
    render(
      <NleClipMenu
        locale="en"
        snapshot={snapshot}
        target={target(clip.clipId, trigger)}
        busy={false}
        rippleEnabled
        onIntent={onIntent}
        onClose={() => undefined}
      />,
    );

    fireEvent.click(
      screen.getByRole("menuitem", { name: "Trim end later by one frame" }),
    );

    expect(firstCommand(onIntent)).toEqual({
      kind: "ripple_trim",
      payload: {
        clip_id: clip.clipId,
        edge: "end",
        delta_frames: 1,
        scope_track_ids: [clip.trackId],
      },
    });
  });
});

describe("M25-79 timeline menus close on an outside press or focus leaving", () => {
  const added: HTMLElement[] = [];
  afterEach(() => added.splice(0).forEach((element) => element.remove()));

  // `opener` is what `NleTimeline` stores as `returnFocus`: a trigger button for a click, the clip
  // or header element for a context-menu gesture.
  function setup(
    kind: "track" | "clip",
    opener: "trigger" | "context",
    busy = false,
  ) {
    const returnFocus = document.createElement(
      opener === "trigger" ? "button" : "div",
    );
    if (opener === "trigger")
      returnFocus.setAttribute("data-h3-nle-menu-trigger", kind);
    else returnFocus.tabIndex = -1;
    const outside = document.createElement("button");
    document.body.append(returnFocus, outside);
    added.push(returnFocus, outside);
    const onIntent = vi.fn(async () => undefined);
    const onClose = vi.fn();
    const props = {
      locale: "en" as const,
      snapshot: snapshotFixture(SMOKE_SHAPE),
      target: {
        id: kind === "track" ? "track-1" : "clip-0",
        returnFocus,
        x: 12,
        y: 16,
      },
      onIntent,
      onClose,
    };
    const element = (isBusy: boolean) =>
      kind === "track" ? (
        <NleTrackMenu {...props} busy={isBusy} />
      ) : (
        <NleClipMenu {...props} busy={isBusy} />
      );
    const view = render(element(busy));
    const menu = screen.getByRole("menu", {
      name: kind === "track" ? "Track menu" : "Clip menu",
    });
    return { view, element, menu, returnFocus, outside, onIntent, onClose };
  }

  it.each([
    ["track", 0],
    ["track", 2],
    ["clip", 0],
    ["clip", 2],
  ] as const)(
    "the %s menu closes on a press of button %i outside it, without a command or a focus pull",
    async (kind, button) => {
      const { menu, outside, onIntent, onClose } = setup(kind, "trigger");
      expect(menu.contains(document.activeElement)).toBe(true);
      fireEvent.pointerDown(outside, { button });
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(onIntent).not.toHaveBeenCalled();
      // `close()` would hand focus back to the trigger on a microtask; a dismissal must not.
      await Promise.resolve();
      expect(menu.contains(document.activeElement)).toBe(true);
    },
  );

  it.each(["track", "clip"] as const)(
    "the %s menu closes when focus moves outside it",
    (kind) => {
      const { outside, onClose } = setup(kind, "context");
      act(() => outside.focus());
      expect(onClose).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["track", "clip"] as const)(
    "the %s menu stays open for presses and focus inside it",
    (kind) => {
      const { menu, onClose } = setup(kind, "trigger");
      const field = menu.querySelector<HTMLElement>("input, select")!;
      fireEvent.pointerDown(field, { button: 0 });
      act(() => field.focus());
      fireEvent.pointerDown(menu, { button: 2 });
      expect(onClose).not.toHaveBeenCalled();
    },
  );

  it.each(["track", "clip"] as const)(
    "a %s menu opened from its trigger button leaves a press on that button to the trigger",
    (kind) => {
      const { returnFocus, onClose } = setup(kind, "trigger");
      fireEvent.pointerDown(returnFocus, { button: 0 });
      act(() => returnFocus.focus());
      expect(onClose).not.toHaveBeenCalled();
    },
  );

  it.each(["track", "clip"] as const)(
    "a %s menu opened by a context-menu gesture closes on a press on the element it opened from",
    (kind) => {
      const { returnFocus, onClose } = setup(kind, "context");
      fireEvent.pointerDown(returnFocus, { button: 0 });
      expect(onClose).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["track", "clip"] as const)(
    "the %s menu stays open while a command elsewhere disables and re-enables its items",
    (kind) => {
      const { view, element, menu, onClose } = setup(kind, "trigger");
      view.rerender(element(true));
      expect(menu.contains(document.activeElement)).toBe(true);
      view.rerender(element(false));
      expect(menu.contains(document.activeElement)).toBe(true);
      expect(onClose).not.toHaveBeenCalled();
    },
  );

  it("removes its listeners when it closes", () => {
    const { view, outside, onClose } = setup("clip", "context");
    view.unmount();
    fireEvent.pointerDown(outside, { button: 0 });
    act(() => outside.focus());
    expect(onClose).not.toHaveBeenCalled();
  });
});
