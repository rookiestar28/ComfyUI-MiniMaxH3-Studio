import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { NleTimeline } from "../src/components/nle/NleTimeline";
import { createNleBinInsertChannel } from "../src/runtime/nleBinInsertChannel";
import { authoringReady, SMOKE_SHAPE } from "./support/nleWorkspaceFixture";
import type { AuthoringIntent } from "../src/state/authoringViewState";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function capturedTimeline() {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  const initial = authoringReady(SMOKE_SHAPE);
  if (initial.status !== "ready" || !initial.timelineHistory)
    throw new Error("fixture must be ready");
  let authoring = initial;
  let snapshot = {
    ...initial.timelineHistory.snapshot,
    clips: [] as typeof initial.timelineHistory.snapshot.clips,
  };
  const channel = createNleBinInsertChannel();
  const onIntent = vi.fn(async (_intent: AuthoringIntent) => undefined);
  const selection: readonly string[] = [];
  const notify = vi.fn();
  const element = () => (
    <div className="h3-nle-dialog">
      <NleTimeline
        locale="en"
        snapshot={snapshot}
        authoring={authoring}
        selection={selection}
        gridFrames={1}
        playheadFrame={0}
        highlightedAssetIds={selection}
        onIntent={onIntent}
        onEdgeGestureActive={notify}
        binInsert={channel}
      />
    </div>
  );
  const view = render(element());
  const grid = view.container.querySelector<HTMLElement>(".h3-nle-tracks")!;
  vi.spyOn(grid, "getBoundingClientRect").mockReturnValue(
    new DOMRect(100, 200, 800, 224),
  );
  const source = snapshot.assets.find((asset) => asset.kind === "video")!;
  act(() => {
    expect(
      channel.begin(7, 0, 0, {
        ...snapshot,
        assetId: source.assetId,
        durationFrames: 24,
      }),
    ).toBe(true);
  });
  return {
    view,
    channel,
    onIntent,
    change(kind: "revision" | "catalog" | "busy" | "mapping" | "unmount") {
      if (kind === "unmount") {
        view.unmount();
        return;
      }
      if (kind === "revision")
        snapshot = {
          ...snapshot,
          workspaceRevision: snapshot.workspaceRevision + 1,
        };
      if (kind === "catalog")
        snapshot = {
          ...snapshot,
          assets: snapshot.assets.filter(
            (asset) => asset.assetId !== source.assetId,
          ),
        };
      if (kind === "busy") authoring = { ...authoring, status: "pending" };
      if (kind === "mapping")
        fireEvent.click(
          view.container.querySelector(
            '[data-h3-nle-control="transport.zoom_in"]',
          )!,
        );
      else view.rerender(element());
    },
  };
}

for (const change of [
  "revision",
  "catalog",
  "busy",
  "mapping",
  "unmount",
] as const) {
  it(`cancels a pending, unpublished draft on ${change} instead of retaining stale ownership`, () => {
    const target = capturedTimeline();
    target.change(change);
    expect(target.channel.activePointer()).toBeNull();
    act(() => {
      target.channel.move(7, 280, 224);
      target.channel.release(7, 280, 224);
    });
    expect(target.onIntent).not.toHaveBeenCalled();
  });
}

it("keeps a captured bin draft across a render that changes no authority or mapping", () => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  const authoring = authoringReady(SMOKE_SHAPE);
  if (authoring.status !== "ready" || !authoring.timelineHistory)
    throw new Error("fixture must be ready");
  const snapshot = { ...authoring.timelineHistory.snapshot, clips: [] };
  const channel = createNleBinInsertChannel();
  const onIntent = vi.fn(async (_intent: AuthoringIntent) => undefined);
  const notify = vi.fn();
  const selection: readonly string[] = [];
  const element = () => (
    <div className="h3-nle-dialog">
      <NleTimeline
        locale="en"
        snapshot={snapshot}
        authoring={authoring}
        selection={selection}
        gridFrames={1}
        playheadFrame={0}
        highlightedAssetIds={selection}
        onIntent={onIntent}
        onEdgeGestureActive={notify}
        binInsert={channel}
      />
    </div>
  );
  const view = render(element());
  const grid = view.container.querySelector<HTMLElement>(".h3-nle-tracks")!;
  vi.spyOn(grid, "getBoundingClientRect").mockReturnValue(
    new DOMRect(100, 200, 800, 224),
  );
  const timeline = view.container.querySelector<HTMLElement>(
    '[data-h3-nle-region="timeline"]',
  )!;
  const source = snapshot.assets.find((asset) => asset.kind === "video")!;
  act(() => {
    expect(
      channel.begin(7, 0, 0, {
        workspaceHandle: snapshot.workspaceHandle,
        workspaceRevision: snapshot.workspaceRevision,
        timelineRevision: snapshot.timelineRevision,
        timelineFingerprint: snapshot.timelineFingerprint,
        assetId: source.assetId,
        durationFrames: 24,
      }),
    ).toBe(true);
  });
  view.rerender(element());
  const scale = Number(timeline.dataset.h3NlePixelsPerFrame);
  const origin = Number(timeline.dataset.h3NleLaneOriginPx);
  act(() => {
    channel.move(7, 100 + origin + 18 * scale, 224);
    channel.release(7, 100 + origin + 18 * scale, 224);
  });
  expect(onIntent).toHaveBeenCalledTimes(1);
  expect(onIntent.mock.calls[0]?.[0]).toMatchObject({
    commands: [
      { kind: "insert_asset_clip", payload: { clip: { start_frame: 18 } } },
    ],
  });
});
