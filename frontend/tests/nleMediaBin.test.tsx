import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  admitPrimaryAppend,
  NleMediaBin,
} from "../src/components/nle/NleMediaBin";
import {
  mediaBinProjection,
  mediaDurationPill,
  mediaDurationTimecode,
  mediaInsertionDurationFrames,
  selectMediaBinProjection,
  virtualMediaRange,
} from "../src/components/nle/nleMediaBinModel";
import type { PublicCompositionAsset } from "../src/contracts/compositionCodec";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import type { AuthoringMediaAssetDecoration } from "../src/host/authoringMediaSourceLease";
import type { NleLeaseScheduler } from "../src/host/nleLeaseScheduler";
import {
  createNleDecorationDemandBroker,
  type NleRoutedDecorationValue,
} from "../src/host/nleDecorationDemandBroker";
import { createNleDecorationCache } from "../src/host/nleDecorationCache";
import {
  AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
  canonicalPublicRuntimeAssetFingerprint,
} from "../src/contracts/authoringMediaLeaseCodec";
import type { AuthoringIntent } from "../src/state/authoringViewState";
import { SMOKE_SHAPE, snapshotFixture } from "./support/nleWorkspaceFixture";

afterEach(cleanup);

// M25-63: range commands live in the card's menu, opened by a context click or Shift+F10 on the
// card. The menu may render outside the bin's own subtree, so it is looked up in the document.
function openAssetMenu(via: "pointer" | "keyboard" = "pointer"): void {
  const card = document.querySelector<HTMLElement>(".h3-nle-media-primary")!;
  if (via === "pointer") fireEvent.contextMenu(card);
  else fireEvent.keyDown(card, { key: "F10", shiftKey: true });
  expect(document.querySelector("[data-h3-nle-asset-menu]")).not.toBeNull();
}

function assetMenuItem(control: string): HTMLButtonElement {
  return document.querySelector<HTMLButtonElement>(
    `[data-h3-nle-asset-menu] [data-h3-nle-control="${control}"]`,
  )!;
}

const asset = (
  assetId: string,
  kind: PublicCompositionAsset["kind"],
  overrides: Partial<PublicCompositionAsset> = {},
): PublicCompositionAsset => ({
  assetId,
  kind,
  sourceTimeBase: kind === "video" ? { num: 1, den: 24 } : null,
  sourceFrameCount: kind === "video" ? 48 : null,
  sourceSampleCount: null,
  embeddedAudio: "absent",
  timestampPolicy:
    kind === "video" ? "nonnegative_monotonic_v1" : "not_applicable",
  landmarks:
    kind === "video"
      ? [
          { frameIndex: 0, pts: 0, dts: 0, durationTicks: 1 },
          { frameIndex: 47, pts: 47, dts: 47, durationTicks: 1 },
        ]
      : [],
  ...overrides,
});

describe("M25-48 media-bin model", () => {
  it("admits primary Add at the primary content end, independent of playhead and overlays", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const primary = original.tracks.find(
      ({ kind }) => kind === "primary_video",
    )!;
    const overlay = original.tracks.find(
      ({ kind }) => kind === "video_overlay",
    )!;
    const source = asset("append-video", "video", {
      sourceFrameCount: 120,
      landmarks: [
        { frameIndex: 0, pts: 0, dts: 0, durationTicks: 1 },
        { frameIndex: 119, pts: 119, dts: 119, durationTicks: 1 },
      ],
    });
    const snapshot = {
      ...original,
      output: { ...original.output, durationFrames: 500 },
      assets: [...original.assets, source],
      tracks: original.tracks.map((track) =>
        track.trackId === primary.trackId
          ? { ...track, order: 1 }
          : track.trackId === overlay.trackId
            ? { ...track, order: 0 }
            : track,
      ),
      clips: [
        {
          ...original.clips[0]!,
          clipId: "primary-a",
          assetId: source.assetId,
          trackId: primary.trackId,
          startFrame: 0,
          durationFrames: 120,
        },
        {
          ...original.clips[0]!,
          clipId: "long-overlay",
          assetId: source.assetId,
          trackId: overlay.trackId,
          startFrame: 0,
          durationFrames: 400,
        },
      ],
    };

    const admitted = admitPrimaryAppend(snapshot, source);
    expect(admitted).toMatchObject({
      admitted: true,
      draft: {
        trackId: primary.trackId,
        startFrame: 120,
        durationFrames: 120,
      },
    });
  });

  it("returns typed Add refusals without truncating or choosing another track", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const primary = original.tracks.find(
      ({ kind }) => kind === "primary_video",
    )!;
    const source = asset("append-video", "video", {
      sourceFrameCount: 120,
      landmarks: [
        { frameIndex: 0, pts: 0, dts: 0, durationTicks: 1 },
        { frameIndex: 119, pts: 119, dts: 119, durationTicks: 1 },
      ],
    });
    const base = {
      ...original,
      output: { ...original.output, durationFrames: 200 },
      assets: [...original.assets, source],
      clips: [
        {
          ...original.clips[0]!,
          assetId: source.assetId,
          trackId: primary.trackId,
          startFrame: 100,
          durationFrames: 80,
        },
      ],
    };

    expect(admitPrimaryAppend(base, source)).toEqual({
      admitted: false,
      reason: "insufficient_capacity",
    });
    expect(
      admitPrimaryAppend(
        {
          ...base,
          tracks: base.tracks.map((track) =>
            track.trackId === primary.trackId
              ? { ...track, locked: true }
              : track,
          ),
        },
        source,
      ),
    ).toEqual({ admitted: false, reason: "primary_locked" });
    expect(
      admitPrimaryAppend(
        { ...base, tracks: base.tracks.filter((track) => track !== primary) },
        source,
      ),
    ).toEqual({ admitted: false, reason: "primary_missing" });
    expect(
      admitPrimaryAppend(base, { ...source, sourceFrameCount: null }),
    ).toEqual({ admitted: false, reason: "timing_unavailable" });
  });

  it("renders a localized typed Add refusal without dispatching", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = {
      ...original,
      clips: [],
      tracks: original.tracks.map((track) =>
        track.kind === "primary_video" ? { ...track, locked: true } : track,
      ),
    };
    const onIntent = vi.fn(async () => undefined);
    const view = render(
      <NleMediaBin
        locale="zh-TW"
        snapshot={snapshot}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => 0}
        onIntent={onIntent}
      />,
    );
    fireEvent.click(
      view.container.querySelector('[data-h3-nle-control="asset.insert"]')!,
    );
    expect(onIntent).not.toHaveBeenCalled();
    expect(view.getByRole("status").textContent).toContain(
      "解除主要視訊軌鎖定",
    );
  });

  it("keeps shared thumbnail routes stable across accepted timeline revisions", () => {
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    const nextSnapshot = {
      ...snapshot,
      timelineRevision: snapshot.timelineRevision + 1,
    };
    const updates: Array<readonly Readonly<{ key: string }>[]> = [];
    const scheduler = {
      updateDecorationDemand(next: readonly Readonly<{ key: string }>[]) {
        updates.push(next);
      },
    } as unknown as Pick<
      NleLeaseScheduler<
        NleRoutedDecorationValue<AuthoringMediaAssetDecoration["value"]>
      >,
      "updateDecorationDemand"
    >;
    const broker = createNleDecorationDemandBroker(scheduler);
    const timeline = broker.register(
      "timeline-filmstrips",
      "filmstrip",
      vi.fn(),
    );
    const view = (value: typeof snapshot) => (
      <NleMediaBin
        locale="en"
        snapshot={value}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={
          {
            acquireAssetDecoration: vi.fn(),
          } as unknown as AuthoringMediaSourceLeaseClient
        }
        decorationDemandBroker={broker}
        currentFrame={() => 0}
        onIntent={vi.fn(async () => undefined)}
      />
    );

    const rendered = render(view(snapshot));
    timeline.update([
      { key: `${snapshot.workspaceHandle}:filmstrip-asset`, acquire: vi.fn() },
    ]);
    const beforeRevision = updates.length;
    const routesBeforeRevision = updates.at(-1);
    expect(updates.at(-1)?.map(({ key }) => key)).toEqual(
      expect.arrayContaining([
        expect.stringContaining("nle-media-bin-thumbnails"),
        expect.stringContaining("timeline-filmstrips"),
      ]),
    );

    rendered.rerender(view(nextSnapshot));

    expect(updates).toHaveLength(beforeRevision + 1);
    expect(
      updates
        .at(-1)
        ?.map((route, index) => route === routesBeforeRevision?.[index]),
    ).toEqual(routesBeforeRevision?.map(() => true));
    expect(updates.at(-1)?.map(({ key }) => key)).toEqual(
      expect.arrayContaining([
        expect.stringContaining("nle-media-bin-thumbnails"),
        expect.stringContaining("timeline-filmstrips"),
      ]),
    );
    rendered.unmount();
    timeline.close();
    broker.close();
  });

  it("searches only public card labels and applies real type, added and order controls", () => {
    const rows = mediaBinProjection([
      asset("private-source.mov", "video"),
      asset("hidden-image.png", "image"),
      asset("other-video.mov", "video"),
    ]);
    const input = {
      query: "",
      filter: "all" as const,
      sort: "ordinal_asc" as const,
      addedAssetIds: new Set(["other-video.mov"]),
      cardTemplate: "Clip {ordinal}",
    };
    expect(
      selectMediaBinProjection(rows, input).map(({ ordinal }) => ordinal),
    ).toEqual([1, 2, 3]);
    expect(
      selectMediaBinProjection(rows, { ...input, query: "private" }),
    ).toEqual([]);
    expect(
      selectMediaBinProjection(rows, { ...input, query: "clip 02" }).map(
        ({ ordinal }) => ordinal,
      ),
    ).toEqual([2]);
    expect(
      selectMediaBinProjection(rows, { ...input, filter: "video" }).map(
        ({ ordinal }) => ordinal,
      ),
    ).toEqual([1, 3]);
    expect(
      selectMediaBinProjection(rows, { ...input, filter: "added" }).map(
        ({ ordinal }) => ordinal,
      ),
    ).toEqual([3]);
    expect(
      selectMediaBinProjection(rows, { ...input, sort: "ordinal_desc" }).map(
        ({ ordinal }) => ordinal,
      ),
    ).toEqual([3, 2, 1]);
  });

  it("filters fonts before assigning stable privacy-safe ordinals", () => {
    const projected = mediaBinProjection([
      asset("raw-video.mov", "video"),
      asset("private-font.ttf", "font"),
      asset("secret-picture.png", "image"),
    ]);
    expect(
      projected.map(({ ordinal, label, asset: value }) => ({
        ordinal,
        label,
        kind: value.kind,
      })),
    ).toEqual([
      { ordinal: 1, label: "Clip 01", kind: "video" },
      { ordinal: 2, label: "Clip 02", kind: "image" },
    ]);
    expect(JSON.stringify(projected.map(({ label }) => label))).not.toMatch(
      /raw-video|secret-picture|private-font/,
    );
  });

  it("formats only public video timing and the still one-frame duration", () => {
    expect(
      mediaDurationTimecode(asset("video", "video"), { num: 24, den: 1 }),
    ).toBe("00:00:02:00");
    expect(
      mediaDurationTimecode(asset("still", "image"), { num: 24, den: 1 }),
    ).toBe("00:00:00:01");
    expect(
      mediaDurationTimecode(
        asset("invalid", "video", { sourceTimeBase: null }),
        { num: 24, den: 1 },
      ),
    ).toBeNull();
  });

  it("M25-63 (B-M2563-04): the duration pill is compact, whole frames below one second", () => {
    const rate = { num: 24, den: 1 };
    expect(mediaDurationPill(asset("still", "image"), rate)).toBe("1f");
    expect(mediaDurationPill(asset("video", "video"), rate)).toBe("00:02");
    const long = (ticks: number) =>
      asset("long", "video", {
        landmarks: [
          { frameIndex: 0, pts: 0, dts: 0, durationTicks: 1 },
          { frameIndex: 47, pts: ticks - 1, dts: ticks - 1, durationTicks: 1 },
        ],
      });
    // 12 source ticks at 1/24 s are half a second: frames, not "00:00".
    expect(mediaDurationPill(long(12), rate)).toBe("12f");
    // 3.5 s shows its whole seconds; the tooltip keeps the frames.
    expect(mediaDurationPill(long(84), rate)).toBe("00:03");
    expect(mediaDurationTimecode(long(84), rate)).toBe("00:00:03:12");
    expect(mediaDurationPill(long(24 * 3605), rate)).toBe("1:00:05");
    expect(
      mediaDurationPill(
        asset("invalid", "video", { sourceTimeBase: null }),
        rate,
      ),
    ).toBeNull();
    expect(mediaDurationPill(asset("video", "video"), null)).toBeNull();
  });

  it("maps a source clock duration onto output frames without exceeding source coverage", () => {
    const imported = asset("imported-video", "video", {
      sourceTimeBase: { num: 1, den: 90_000 },
      sourceFrameCount: 124,
      landmarks: [
        { frameIndex: 0, pts: 0, dts: 0, durationTicks: 3_000 },
        {
          frameIndex: 123,
          pts: 369_000,
          dts: 369_000,
          durationTicks: 3_000,
        },
      ],
    });
    expect(mediaInsertionDurationFrames(imported, { num: 24, den: 1 })).toBe(
      99,
    );
    expect(mediaInsertionDurationFrames(imported, { num: 30, den: 1 })).toBe(
      124,
    );
  });

  it("mounts visible rows plus exactly one overscan row on each side", () => {
    expect(
      virtualMediaRange({
        itemCount: 100,
        viewportWidth: 440,
        viewportHeight: 300,
        scrollTop: 300,
        minimumCardWidth: 200,
        rowHeight: 150,
      }),
    ).toEqual({
      columns: 2,
      visibleRows: 2,
      startIndex: 2,
      endIndex: 10,
      topSpacerPx: 150,
      bottomSpacerPx: 6_750,
      mountedLimit: 8,
    });
  });

  it("renders privacy-safe cards and dispatches one deduplicated primary append", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = { ...original, clips: [] };
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    const view = render(
      <NleMediaBin
        locale="en"
        snapshot={snapshot}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => 24}
        onIntent={onIntent}
      />,
    );
    expect(
      view.container.querySelector('[data-h3-nle-asset="vid-primary"]'),
    ).not.toBeNull();
    expect(
      view.container
        .querySelector(
          '[data-h3-nle-asset="vid-primary"] .h3-nle-media-primary',
        )!
        .getAttribute("aria-label"),
    ).toMatch(/Clip 01/);
    expect(
      view.container
        .querySelector(
          '[data-h3-nle-asset="vid-primary"] [data-h3-nle-control="asset.insert"]',
        )!
        .getAttribute("aria-label"),
    ).toBe("Add Clip 01 to the timeline");
    expect(view.container.textContent).not.toContain(
      snapshot.assets[0]!.assetId,
    );
    expect(view.container.textContent).not.toContain(
      snapshot.assets.find(({ kind }) => kind === "font")!.assetId,
    );
    for (const element of view.container.querySelectorAll(
      "[aria-label],[title]",
    )) {
      const exposed = `${element.getAttribute("aria-label") ?? ""} ${element.getAttribute("title") ?? ""}`;
      for (const value of snapshot.assets.map(({ assetId }) => assetId))
        expect(exposed).not.toContain(value);
    }
    fireEvent.click(
      view.container.querySelector<HTMLButtonElement>(
        '[data-h3-nle-control="asset.insert"]',
      )!,
    );
    fireEvent.click(
      view.container.querySelector<HTMLButtonElement>(
        '[data-h3-nle-control="asset.insert"]',
      )!,
    );
    expect(onIntent).toHaveBeenCalledTimes(1);
    expect(onIntent.mock.calls[0]![0]).toMatchObject({
      action: "apply_timeline_commands",
      commands: [
        {
          kind: "insert_asset_clip",
          payload: { clip: { start_frame: 0 } },
        },
      ],
    });
    expect(view.getByRole("status").textContent).toContain(
      "Adding clip to the end",
    );
    expect(view.container.querySelector('[draggable="true"]')).toBeNull();
  });

  it("admits one V1 image Add on the image track at the playhead", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = { ...original, clips: [] };
    const image = snapshot.assets.find(({ kind }) => kind === "image")!;
    const track = snapshot.tracks.find(({ kind }) => kind === "image_overlay")!;
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    const view = render(
      <NleMediaBin
        locale="en"
        snapshot={snapshot}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => 24}
        onIntent={onIntent}
      />,
    );
    fireEvent.click(
      view.container.querySelector<HTMLButtonElement>(
        `[data-h3-nle-asset="${image.assetId}"] [data-h3-nle-control="asset.insert"]`,
      )!,
    );
    expect(onIntent).toHaveBeenCalledTimes(1);
    expect(onIntent.mock.calls[0]![0]).toMatchObject({
      action: "apply_timeline_commands",
      commands: [
        {
          kind: "insert_asset_clip",
          payload: {
            clip: {
              asset_id: image.assetId,
              track_id: track.trackId,
              start_frame: 24,
            },
          },
        },
      ],
    });
  });

  it("does not call a newly imported but uninserted asset added to the timeline", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = { ...original, clips: [] };
    const view = render(
      <NleMediaBin
        locale="en"
        snapshot={snapshot}
        highlightedAssetIds={[snapshot.assets[0]!.assetId]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => 0}
        onIntent={vi.fn(async () => undefined)}
      />,
    );
    expect(
      view.container.querySelectorAll("[data-h3-nle-asset]").length,
    ).toBeGreaterThan(0);
    view.container.querySelector<HTMLElement>(
      ".h3-nle-media-scroll",
    )!.scrollTo = vi.fn();
    fireEvent.click(
      view.container.querySelector(
        '[data-h3-nle-control="media.sort_filter"]',
      )!,
    );
    fireEvent.click(
      document.querySelector(
        '[data-h3-nle-control="media.filter"] [data-h3-nle-value="added"]',
      )!,
    );
    expect(view.container.querySelector("[data-h3-nle-asset]")).toBeNull();
  });

  it("uses Enter plus explicit range alternatives without issuing on pointer movement", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = { ...original, clips: [] };
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    const view = render(
      <NleMediaBin
        locale="en"
        snapshot={snapshot}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => 12}
        onIntent={onIntent}
      />,
    );
    const card = view.container.querySelector<HTMLElement>(
      ".h3-nle-media-primary",
    )!;
    fireEvent.pointerMove(card, { clientX: 20, clientY: 20 });
    expect(onIntent).not.toHaveBeenCalled();
    fireEvent.keyDown(card, { key: "Enter" });
    expect(onIntent).toHaveBeenCalledTimes(1);
    // Nothing is sent by opening the menu.
    openAssetMenu("pointer");
    expect(onIntent).toHaveBeenCalledTimes(1);
    fireEvent.click(assetMenuItem("range.insert"));
    expect(document.querySelector("[data-h3-nle-asset-menu]")).toBeNull();
    openAssetMenu("keyboard");
    fireEvent.click(assetMenuItem("range.overwrite"));
    expect(onIntent.mock.calls.slice(1).map(([intent]) => intent)).toEqual([
      expect.objectContaining({
        commands: [expect.objectContaining({ kind: "insert_range" })],
      }),
      expect.objectContaining({
        commands: [expect.objectContaining({ kind: "overwrite_range" })],
      }),
    ]);
  });

  it("appends Add while interior Insert refuses and Overwrite remains explicit", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const primary = original.tracks.find(
      ({ kind }) => kind === "primary_video",
    )!;
    const occupiedSource = original.clips.find(
      ({ trackId }) => trackId === primary.trackId,
    )!;
    const occupied = { ...occupiedSource, durationFrames: 200 };
    const snapshot = {
      ...original,
      tracks: [primary],
      clips: [occupied],
    };
    const interior = occupied.startFrame + 1;
    let playhead = interior;
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    const view = render(
      <NleMediaBin
        locale="en"
        snapshot={snapshot}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => playhead}
        onIntent={onIntent}
      />,
    );

    fireEvent.click(
      view.container.querySelector<HTMLButtonElement>(
        '[data-h3-nle-control="asset.insert"]',
      )!,
    );
    expect(onIntent).toHaveBeenCalledTimes(1);
    expect(onIntent.mock.calls[0]![0]).toMatchObject({
      commands: [
        {
          kind: "insert_asset_clip",
          payload: { clip: { start_frame: 200, track_id: primary.trackId } },
        },
      ],
    });

    openAssetMenu();
    fireEvent.click(assetMenuItem("range.insert"));
    expect(onIntent).toHaveBeenCalledTimes(1);
    openAssetMenu();
    fireEvent.click(assetMenuItem("range.overwrite"));
    playhead = occupied.startFrame + occupied.durationFrames;
    openAssetMenu();
    fireEvent.click(assetMenuItem("range.insert"));

    expect(onIntent).toHaveBeenCalledTimes(3);
    expect(onIntent.mock.calls.slice(1).map(([intent]) => intent)).toEqual([
      expect.objectContaining({
        commands: [
          expect.objectContaining({
            kind: "overwrite_range",
            payload: expect.objectContaining({
              clip: expect.objectContaining({
                track_id: primary.trackId,
                start_frame: interior,
              }),
              scope_track_ids: [primary.trackId],
              remainder_ids: expect.objectContaining({
                [occupied.clipId]: expect.any(String),
              }),
            }),
          }),
        ],
      }),
      expect.objectContaining({
        commands: [
          expect.objectContaining({
            kind: "insert_range",
            payload: expect.objectContaining({
              clip: expect.objectContaining({
                track_id: primary.trackId,
                start_frame: playhead,
              }),
              scope_track_ids: [primary.trackId],
            }),
          }),
        ],
      }),
    ]);
  });

  it("keeps workspace-owned thumbnails warm when the Media pane unmounts", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = { ...original, clips: [] };
    const asset = snapshot.assets.find(({ kind }) => kind === "video")!;
    const bitmap = {
      width: 32,
      height: 18,
      close: vi.fn(),
    } as unknown as ImageBitmap;
    const cache = createNleDecorationCache();
    cache.set(
      {
        workspaceHandle: snapshot.workspaceHandle,
        assetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
        derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
        derivativeKind: "thumbnail",
      },
      bitmap,
    );
    const drawImage = vi.fn();
    const getContext = vi
      .spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
    const renderBin = () =>
      render(
        <NleMediaBin
          locale="en"
          snapshot={snapshot}
          highlightedAssetIds={[]}
          busy={false}
          runtimeEpoch={1}
          leaseClient={{} as AuthoringMediaSourceLeaseClient}
          decorationCache={cache}
          currentFrame={() => 0}
          onIntent={vi.fn(async () => undefined)}
        />,
      );

    let view = renderBin();
    const firstCanvas = view.container.querySelector<HTMLCanvasElement>(
      `[data-h3-nle-asset="${asset.assetId}"] [data-h3-nle-thumbnail]`,
    )!;
    expect([firstCanvas.width, firstCanvas.height]).toEqual([32, 18]);
    expect(
      view.container.querySelector(".h3-nle-thumbnail-status")?.textContent,
    ).toBe("");
    expect(drawImage).toHaveBeenCalledWith(bitmap, 0, 0);

    view.unmount();
    expect(bitmap.close).not.toHaveBeenCalled();

    view = renderBin();
    const remountedCanvas = view.container.querySelector<HTMLCanvasElement>(
      `[data-h3-nle-asset="${asset.assetId}"] [data-h3-nle-thumbnail]`,
    )!;
    expect([remountedCanvas.width, remountedCanvas.height]).toEqual([32, 18]);
    expect(
      view.container.querySelector(".h3-nle-thumbnail-status")?.textContent,
    ).toBe("");
    expect(drawImage).toHaveBeenCalledTimes(2);

    view.unmount();
    cache.close();
    expect(bitmap.close).toHaveBeenCalledTimes(1);
    getContext.mockRestore();
  });

  it("closes revoked bitmaps and clears reused canvas pixels before replacement paint", () => {
    const original = snapshotFixture(SMOKE_SHAPE);
    const snapshot = { ...original, clips: [] };
    let publish:
      ((value: AuthoringMediaAssetDecoration["value"]) => void) | undefined;
    const scheduler = {
      updateDecorationDemand: vi.fn((_demands, nextPublish) => {
        publish = nextPublish;
      }),
      snapshot: () => ({
        activeKey: null,
        pendingKeys: [],
        idleAfterBackoff: false,
        closed: false,
      }),
      whenIdle: async () => undefined,
      close: vi.fn(),
      acquirePlayback: async <R,>(operation: () => Promise<R>) => operation(),
    } as NleLeaseScheduler<AuthoringMediaAssetDecoration["value"]>;
    const drawImage = vi.fn();
    const clearRect = vi.fn();
    const getContext = vi
      .spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockReturnValue({
        drawImage,
        clearRect,
      } as unknown as CanvasRenderingContext2D);
    const onIntent = vi.fn<(intent: AuthoringIntent) => Promise<void>>(
      async () => undefined,
    );
    const view = render(
      <NleMediaBin
        locale="en"
        snapshot={snapshot}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        leaseScheduler={scheduler}
        currentFrame={() => 0}
        onIntent={onIntent}
      />,
    );
    const firstAsset = snapshot.assets.find(({ kind }) => kind === "video")!;
    const firstFingerprint = canonicalPublicRuntimeAssetFingerprint(firstAsset);
    const firstBitmap = {
      width: 32,
      height: 18,
      close: vi.fn(),
    } as unknown as ImageBitmap;
    act(() =>
      publish?.({
        bitmap: firstBitmap,
        derivativeKind: "thumbnail",
        geometry: {
          schema: "h3.authoring.media_geometry.v1",
          sourceWidth: 32,
          sourceHeight: 18,
          derivativeWidth: 32,
          derivativeHeight: 18,
        },
        tileCount: 1,
        cacheKey: {
          workspaceHandle: snapshot.workspaceHandle,
          assetFingerprint: firstFingerprint,
          derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
          derivativeKind: "thumbnail",
        },
      }),
    );
    const canvas = view.container.querySelector<HTMLCanvasElement>(
      '[data-h3-nle-asset="vid-primary"] canvas',
    )!;
    expect(drawImage).toHaveBeenCalledWith(firstBitmap, 0, 0);
    expect([canvas.width, canvas.height]).toEqual([32, 18]);

    view.rerender(
      <NleMediaBin
        locale="en"
        snapshot={snapshot}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={2}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        leaseScheduler={scheduler}
        currentFrame={() => 0}
        onIntent={onIntent}
      />,
    );
    expect(firstBitmap.close).toHaveBeenCalledTimes(1);
    expect([canvas.width, canvas.height]).toEqual([0, 0]);

    const secondBitmap = {
      width: 40,
      height: 22,
      close: vi.fn(),
    } as unknown as ImageBitmap;
    act(() =>
      publish?.({
        bitmap: secondBitmap,
        derivativeKind: "thumbnail",
        geometry: {
          schema: "h3.authoring.media_geometry.v1",
          sourceWidth: 40,
          sourceHeight: 22,
          derivativeWidth: 40,
          derivativeHeight: 22,
        },
        tileCount: 1,
        cacheKey: {
          workspaceHandle: snapshot.workspaceHandle,
          assetFingerprint: firstFingerprint,
          derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
          derivativeKind: "thumbnail",
        },
      }),
    );
    const changedSnapshot = {
      ...snapshot,
      assets: snapshot.assets.map((member) =>
        member.assetId === firstAsset.assetId
          ? {
              ...member,
              sourceFrameCount: (member.sourceFrameCount ?? 1) + 1,
            }
          : member,
      ),
    };
    view.rerender(
      <NleMediaBin
        locale="en"
        snapshot={changedSnapshot}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={2}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        leaseScheduler={scheduler}
        currentFrame={() => 0}
        onIntent={onIntent}
      />,
    );
    expect(secondBitmap.close).toHaveBeenCalledTimes(1);
    expect([canvas.width, canvas.height]).toEqual([0, 0]);
    getContext.mockRestore();
  });
});
