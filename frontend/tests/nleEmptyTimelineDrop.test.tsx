import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { NleEmptyTimelineDrop } from "../src/components/nle/NleEmptyTimelineDrop";
import {
  build,
  freshIdentifier,
} from "../src/components/nle/nleCommandBuilders";
import {
  decodeTimelineHistoryProjectionV2,
  type NleAuthoringStateV2,
} from "../src/contracts/authoringWorkbenchCodec";
import { createNleBinInsertChannel } from "../src/runtime/nleBinInsertChannel";
import { IDENTITY_CLIP_AUDIO } from "../src/contracts/compositionCodec";
import type { AuthoringIntent } from "../src/state/authoringViewState";
import { importV2ResponseWire } from "./support/productionAuthoringImportFixture";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function setup(
  configure: (state: NleAuthoringStateV2) => NleAuthoringStateV2 = (state) =>
    state,
) {
  let authoring = configure(
    decodeTimelineHistoryProjectionV2(importV2ResponseWire().history_projection)
      .authoring,
  );
  let busy = false;
  const channel = createNleBinInsertChannel();
  const onIntent = vi.fn(async (_intent: AuthoringIntent) => undefined);
  const element = () => (
    <NleEmptyTimelineDrop
      locale="en"
      authoring={authoring}
      busy={busy}
      channel={channel}
      onIntent={onIntent}
    />
  );
  const view = render(element());
  const grid = view.container.querySelector<HTMLDivElement>(
    "[data-h3-nle-empty-drop]",
  )!;
  vi.spyOn(grid, "getBoundingClientRect").mockReturnValue(
    new DOMRect(100, 200, 800, 168),
  );
  const assetId = authoring.assets[0]!.assetId;
  const begin = (pointerId = 7) => {
    let admitted = false;
    act(() => {
      admitted = channel.begin(pointerId, 10, 10, {
        ...authoring,
        assetId,
        durationFrames: 24,
      });
    });
    return admitted;
  };
  const drop = (frame = 18, row = 0, x?: number) => {
    const clientX = x ?? 100 + Number(grid.dataset.h3NleLaneOriginPx) + frame;
    act(() => {
      channel.move(7, clientX, 224 + row * 56);
      channel.release(7, clientX, 224 + row * 56);
    });
  };
  return {
    view,
    channel,
    onIntent,
    grid,
    begin,
    drop,
    state: () => authoring,
    rerender(next = authoring, nextBusy = busy) {
      authoring = next;
      busy = nextBusy;
      view.rerender(element());
    },
  };
}

it("inserts from actual empty authoring and shares the equal-placement clip builder", () => {
  const target = setup();
  expect(target.begin()).toBe(true);
  target.rerender();
  target.drop();
  const authoring = target.state();
  expect(target.onIntent).toHaveBeenCalledExactlyOnceWith({
    action: "apply_timeline_commands",
    capturedTimeline: {
      workspaceHandle: authoring.workspaceHandle,
      workspaceRevision: authoring.workspaceRevision,
      timelineRevision: authoring.timelineRevision,
      timelineFingerprint: authoring.timelineFingerprint,
      authoringFingerprint: authoring.authoringFingerprint,
    },
    commands: [
      build.insertAssetClip({
        clipId: freshIdentifier(authoring, "clip"),
        assetId: authoring.assets[0]!.assetId,
        trackId: authoring.tracks[0]!.trackId,
        startFrame: 18,
        durationFrames: 24,
        sourceStartFrame: 0,
        text: null,
      }),
    ],
  });
  expect(target.channel.activePointer()).toBeNull();
});

for (const kind of [
  "locked",
  "incompatible",
  "outside",
  "full",
  "overlap",
] as const) {
  it(`visibly refuses an empty receiver ${kind} target without a transaction`, () => {
    const target = setup((authoring) => {
      const tracks = authoring.tracks.map((track) => ({
        ...track,
        locked: kind === "locked",
        kind: kind === "incompatible" ? ("text_overlay" as const) : track.kind,
      }));
      const clip = {
        clipId: "occupied",
        trackId: tracks[0]!.trackId,
        assetId: authoring.assets[0]!.assetId,
        startFrame: 18,
        durationFrames: 24,
        sourceStartFrame: 0,
        enabled: true,
        transform: {},
        crop: {},
        opacityBp: 10000,
        blend: "normal" as const,
        text: null,
        transition: { kind: "cut" as const, durationFrames: 0 },
        effect: {},
        audio: IDENTITY_CLIP_AUDIO,
      };
      return {
        ...authoring,
        tracks,
        clips:
          kind === "full"
            ? Array.from({ length: 128 }, (_, i) => ({
                ...clip,
                clipId: `clip-${i}`,
              }))
            : kind === "overlap"
              ? [clip]
              : [],
      };
    });
    expect(target.begin()).toBe(true);
    target.drop(18, 0, kind === "outside" ? 99 : undefined);
    expect(target.onIntent).not.toHaveBeenCalled();
    expect(
      target.view.container.querySelector('[role="status"]')!.textContent,
    ).toMatch(/Cannot|cannot|refused|locked|track|overlap/i);
    expect(target.channel.activePointer()).toBeNull();
  });
}

for (const change of ["busy", "revision", "fingerprint", "unmount"] as const) {
  it(`cancels its captured pointer on ${change}`, () => {
    const target = setup();
    expect(target.begin()).toBe(true);
    if (change === "unmount") target.view.unmount();
    else
      target.rerender(
        {
          ...target.state(),
          workspaceRevision:
            target.state().workspaceRevision + (change === "revision" ? 1 : 0),
          authoringFingerprint:
            change === "fingerprint"
              ? `sha256:${"f".repeat(64)}`
              : target.state().authoringFingerprint,
        },
        change === "busy",
      );
    expect(target.channel.activePointer()).toBeNull();
    target.drop();
    expect(target.onIntent).not.toHaveBeenCalled();
  });
}

it("clips the source at real edit capacity and converts scrolled lane coordinates", () => {
  const target = setup((authoring) => ({
    ...authoring,
    editCapacityFrames: 60,
  }));
  target.grid.scrollLeft = 30;
  expect(target.begin()).toBe(true);
  target.drop(18);
  expect(target.onIntent.mock.calls[0]![0]).toMatchObject({
    commands: [
      {
        kind: "insert_asset_clip",
        payload: { clip: { start_frame: 48, duration_frames: 12 } },
      },
    ],
  });
});

it("refuses stale authoring identity synchronously and leaves no receiver after unmount", () => {
  const target = setup();
  act(() => {
    expect(
      target.channel.begin(7, 0, 0, {
        ...target.state(),
        assetId: target.state().assets[0]!.assetId,
        durationFrames: 24,
        authoringFingerprint: `sha256:${"f".repeat(64)}`,
      }),
    ).toBe(false);
  });
  expect(target.onIntent).not.toHaveBeenCalled();
  target.view.unmount();
  expect(target.begin()).toBe(false);
});

it("clears the insertion announcement with a cancelled ghost", () => {
  const target = setup();
  expect(target.begin()).toBe(true);
  act(() => {
    target.channel.move(7, 246, 224);
  });
  expect(
    target.view.container.querySelector('[role="status"]')!.textContent,
  ).toMatch(/Insert/);
  act(() => {
    target.channel.cancel("escape", 7);
  });
  expect(
    target.view.container.querySelector('[role="status"]')!.textContent,
  ).toBe("");
  expect(
    target.view.container.querySelector("[data-h3-nle-insert-ghost]"),
  ).toBeNull();
  expect(target.onIntent).not.toHaveBeenCalled();
});
