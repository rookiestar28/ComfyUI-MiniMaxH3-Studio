import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { Profiler } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthoringIntent } from "../src/state/authoringViewState";
import { NleTextBin } from "../src/components/nle/NleTextBin";
import { SMOKE_SHAPE, snapshotFixture } from "./support/nleWorkspaceFixture";
import { nleCopy } from "../src/components/nle/nleCopy";

afterEach(cleanup);

describe("M25-48 Text bin", () => {
  it.each(["en", "zh-TW", "zh-CN"] as const)(
    "disables an exhausted track pool with current %s refusal copy and no intent",
    (locale) => {
      const original = snapshotFixture(SMOKE_SHAPE);
      const tracks = Array.from({ length: 8 }, (_, order) => ({
        trackId: `track.${order}`,
        kind:
          order === 0 ? ("primary_video" as const) : ("video_overlay" as const),
        order,
        enabled: true,
        locked: false,
      }));
      const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
        async () => undefined,
      );
      const view = render(
        <NleTextBin
          locale={locale}
          snapshot={{ ...original, tracks, clips: [] }}
          busy={false}
          currentFrame={() => 0}
          onIntent={onIntent}
        />,
      );
      const button = view.container.querySelector<HTMLButtonElement>(
        '[data-h3-nle-control="title.insert"]',
      )!;
      expect(button.disabled).toBe(true);
      expect(view.getByRole("status").textContent).toBe(
        nleCopy(locale).assets.titleRefusals.track_limit,
      );
      expect(button.getAttribute("aria-describedby")).toBe(
        view.getByRole("status").id,
      );
      fireEvent.click(button);
      expect(onIntent).not.toHaveBeenCalled();
    },
  );

  it("derives the initial default from a late catalog without a reseeding effect", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = { ...original, clips: [] };
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    const props = {
      locale: "en" as const,
      busy: false,
      currentFrame: () => 0,
      onIntent,
    };
    const view = render(
      <NleTextBin
        {...props}
        snapshot={{
          ...snapshot,
          assets: snapshot.assets.filter((asset) => asset.kind !== "font"),
        }}
      />,
    );
    expect(
      (view.getByRole("button", { name: "Add title" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    view.rerender(<NleTextBin {...props} snapshot={snapshot} />);
    const button = view.getByRole("button", { name: "Add title" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);
    expect(onIntent).toHaveBeenCalledTimes(1);
    expect(onIntent.mock.calls[0]![0]).toMatchObject({
      commands: [
        {
          payload: {
            clip: {
              text: {
                font_asset_id: snapshot.assets.find(
                  (asset) => asset.kind === "font",
                )!.assetId,
              },
            },
          },
        },
      ],
    });
  });

  it("retains an explicit removed font as a refusal instead of silently switching fonts", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const font = original.assets.find((asset) => asset.kind === "font")!;
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    const props = {
      locale: "en" as const,
      busy: false,
      currentFrame: () => 0,
      onIntent,
    };
    const view = render(
      <NleTextBin
        {...props}
        snapshot={{
          ...original,
          clips: [],
          assets: [...original.assets, { ...font, assetId: "font.second" }],
        }}
      />,
    );
    fireEvent.change(view.getByRole("combobox"), {
      target: { value: "font.second" },
    });
    view.rerender(
      <NleTextBin {...props} snapshot={{ ...original, clips: [] }} />,
    );
    expect(
      (view.getByRole("button", { name: "Add title" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(view.getByRole("status").textContent).toBe(
      nleCopy("en").assets.titleRefusals.missing_font,
    );
    expect((view.getByRole("combobox") as HTMLSelectElement).value).toBe(
      "font.second",
    );
    fireEvent.click(view.getByRole("button", { name: "Add title" }));
    expect(onIntent).not.toHaveBeenCalled();
  });

  it("unchanged admission ticks publish no render, dispatch uses the current frame, and a refusal transition stays local", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const title = original.clips.find((clip) => clip.text !== null)!;
    const tracks = Array.from({ length: 8 }, (_, order) => ({
      trackId: `track.${order}`,
      kind:
        order === 0
          ? ("primary_video" as const)
          : order === 7
            ? ("text_overlay" as const)
            : ("video_overlay" as const),
      order,
      enabled: true,
      locked: false,
    }));
    const snapshot = {
      ...original,
      tracks,
      clips: [
        { ...title, trackId: "track.7", startFrame: 0, durationFrames: 24 },
      ],
    };
    let frame = 48;
    let parentRenders = 0;
    const commits = vi.fn();
    const listeners = new Set<() => void>();
    const subscribePlayhead = (listener: () => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    };
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    function Parent() {
      parentRenders += 1;
      return (
        <Profiler id="text" onRender={commits}>
          <NleTextBin
            locale="en"
            snapshot={snapshot}
            busy={false}
            currentFrame={() => frame}
            subscribePlayhead={subscribePlayhead}
            onIntent={onIntent}
          />
        </Profiler>
      );
    }
    const view = render(<Parent />);
    commits.mockClear();
    for (const next of [49, 50, 51, 55])
      act(() => {
        frame = next;
        listeners.forEach((listener) => listener());
      });
    expect(commits).not.toHaveBeenCalled();
    expect(parentRenders).toBe(1);
    fireEvent.click(view.getByRole("button", { name: "Add title" }));
    expect(onIntent.mock.calls[0]![0]).toMatchObject({
      commands: [
        { payload: { clip: { start_frame: 55, track_id: "track.7" } } },
      ],
    });
    act(() => {
      frame = 0;
      listeners.forEach((listener) => listener());
    });
    expect(
      (view.getByRole("button", { name: "Add title" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(view.getByRole("status").textContent).toBe(
      nleCopy("en").assets.titleRefusals.track_limit,
    );
    expect(commits).toHaveBeenCalledTimes(1);
    expect(parentRenders).toBe(1);
    fireEvent.click(view.getByRole("button", { name: "Add title" }));
    expect(onIntent).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(listeners.size).toBe(0);
  });
  it("allocates a missing text track in the same title intent", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = {
      ...original,
      clips: [],
      tracks: original.tracks.filter((track) => track.kind !== "text_overlay"),
    };
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    const view = render(
      <NleTextBin
        locale="en"
        snapshot={snapshot}
        busy={false}
        currentFrame={() => 18}
        onIntent={onIntent}
      />,
    );
    fireEvent.click(
      view.container.querySelector<HTMLButtonElement>(
        '[data-h3-nle-control="title.insert"]',
      )!,
    );
    expect(onIntent).toHaveBeenCalledTimes(1);
    const intent = onIntent.mock.calls[0]![0];
    expect(intent).toMatchObject({
      action: "apply_timeline_commands",
      commands: [{ kind: "create_track" }, { kind: "insert_title_clip" }],
    });
  });

  it("shows ordinal font copy and dispatches exactly one title insert at playhead", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = { ...original, clips: [] };
    const font = snapshot.assets.find(({ kind }) => kind === "font")!;
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    const view = render(
      <NleTextBin
        locale="en"
        snapshot={snapshot}
        busy={false}
        currentFrame={() => 18}
        onIntent={onIntent}
      />,
    );
    expect(view.container.textContent).not.toContain(font.assetId);
    fireEvent.change(view.getByLabelText("Title text"), {
      target: { value: "Opening card" },
    });
    fireEvent.click(
      view.container.querySelector<HTMLButtonElement>(
        '[data-h3-nle-control="title.insert"]',
      )!,
    );
    expect(onIntent).toHaveBeenCalledTimes(1);
    expect(onIntent.mock.calls[0]![0]).toMatchObject({
      action: "apply_timeline_commands",
      commands: [
        {
          kind: "insert_title_clip",
          payload: {
            clip: {
              asset_id: null,
              start_frame: 18,
              text: {
                content: "Opening card",
                font_asset_id: font.assetId,
              },
            },
          },
        },
      ],
    });
  });
});
