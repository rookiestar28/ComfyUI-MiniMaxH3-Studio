import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
  type NleAuthoringStateV2,
} from "../src/contracts/authoringWorkbenchCodec";
import {
  IDENTITY_CLIP_AUDIO,
  type PublicCompositionAsset,
} from "../src/contracts/compositionCodec";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaSourceLeaseClient,
} from "../src/host/authoringMediaSourceLease";
import type { NleDecorationDemandBroker } from "../src/host/nleDecorationDemandBroker";
import type { NleDecorationDemand } from "../src/host/nleLeaseScheduler";
import { buildNleAuthoringAssetManifest } from "../src/runtime/nleAuthoringAssetManifest";
import type { AuthoringIntent } from "../src/state/authoringViewState";
import {
  admitAuthoringPrimaryAppend,
  NleAuthoringAssetBin,
} from "../src/components/nle/NleAuthoringAssetBin";

afterEach(() => cleanup());

const video: PublicCompositionAsset = Object.freeze({
  assetId: "asset.video",
  kind: "video",
  sourceTimeBase: Object.freeze({ num: 1, den: 24 }),
  sourceFrameCount: 48,
  sourceSampleCount: null,
  embeddedAudio: "absent",
  timestampPolicy: "nonnegative_monotonic_v1",
  landmarks: Object.freeze([
    Object.freeze({ frameIndex: 0, pts: 0, dts: 0, durationTicks: 1 }),
    Object.freeze({ frameIndex: 47, pts: 47, dts: 47, durationTicks: 1 }),
  ]),
});

const image: PublicCompositionAsset = Object.freeze({
  assetId: "asset.image",
  kind: "image",
  sourceTimeBase: null,
  sourceFrameCount: null,
  sourceSampleCount: null,
  embeddedAudio: "absent",
  timestampPolicy: "nonnegative_monotonic_v1",
  landmarks: Object.freeze([]),
});

function emptyAuthoring(): NleAuthoringStateV2 {
  return {
    schema: NLE_AUTHORING_SCHEMA,
    profileId: NLE_AUTHORING_PROFILE_ID,
    operationProfileId: NLE_OPERATION_PROFILE_ID_V2,
    projectId: "project.1",
    workspaceHandle: "workspace.1",
    workspaceRevision: 3,
    workspaceFingerprint: `sha256:${"1".repeat(64)}`,
    timelineRevision: 4,
    timelineFingerprint: `sha256:${"2".repeat(64)}`,
    authoringFingerprint: `sha256:${"3".repeat(64)}`,
    editCapacityFrames: 3_600,
    contentEndExclusive: 0,
    assets: [video],
    tracks: [
      {
        trackId: "track.primary",
        kind: "primary_video",
        order: 0,
        enabled: true,
        locked: false,
      },
    ],
    clips: [],
    audioExtension: Object.freeze({}),
    blockers: [],
  };
}

describe("NleAuthoringAssetBin", () => {
  it("appends exact 124-frame clips to primary content, not playhead or overlay extent", () => {
    const source: PublicCompositionAsset = {
      ...video,
      sourceFrameCount: 124,
      landmarks: [
        { frameIndex: 0, pts: 0, dts: 0, durationTicks: 1 },
        { frameIndex: 123, pts: 123, dts: 123, durationTicks: 1 },
      ],
    };
    const base = emptyAuthoring();
    const primaryClip = {
      clipId: "primary-1",
      assetId: source.assetId,
      trackId: "track.primary",
      startFrame: 0,
      durationFrames: 124,
      sourceStartFrame: 0,
      enabled: true,
      transform: {},
      crop: {},
      opacityBp: 10_000,
      blend: "normal" as const,
      text: null,
      transition: { kind: "cut", durationFrames: 0 },
      effect: {},
      audio: IDENTITY_CLIP_AUDIO,
    };
    const authoring: NleAuthoringStateV2 = {
      ...base,
      editCapacityFrames: 1_000,
      contentEndExclusive: 900,
      assets: [source],
      tracks: [
        ...base.tracks.map((track) => ({ ...track, order: 1 })),
        {
          trackId: "track.overlay",
          kind: "video_overlay",
          order: 0,
          enabled: true,
          locked: false,
        },
      ],
      clips: [
        primaryClip,
        {
          ...primaryClip,
          clipId: "overlay-long",
          trackId: "track.overlay",
          durationFrames: 900,
        },
      ],
    };
    expect(admitAuthoringPrimaryAppend(authoring, source)).toMatchObject({
      admitted: true,
      draft: {
        trackId: "track.primary",
        startFrame: 124,
        durationFrames: 124,
      },
    });
  });

  it("requests visible thumbnails with the current V2 authoring manifest", async () => {
    const authoring = emptyAuthoring();
    let activeDemands: readonly NleDecorationDemand<
      AuthoringMediaAssetDecoration["value"]
    >[] = [];
    const decorationDemandBroker = {
      register: vi.fn((_key, _priority, _publish) => ({
        update(demands: typeof activeDemands) {
          activeDemands = demands;
        },
        close: vi.fn(),
      })),
    } as unknown as NleDecorationDemandBroker<
      AuthoringMediaAssetDecoration["value"]
    >;
    const acquired = {
      value: {
        bitmap: { width: 32, height: 18, close: vi.fn() },
        derivativeKind: "thumbnail" as const,
        geometry: {
          schema: "h3.authoring.media_geometry.v1" as const,
          sourceWidth: 1920,
          sourceHeight: 1080,
          derivativeWidth: 32,
          derivativeHeight: 18,
        },
        tileCount: 1,
        cacheKey: {
          workspaceHandle: authoring.workspaceHandle,
          assetFingerprint: "sha256:test",
          derivativeProfileId: "h3.authoring.media_derivatives.v6",
          derivativeKind: "thumbnail" as const,
        },
      },
      release: vi.fn(async () => undefined),
      discard: vi.fn(),
    } as unknown as AuthoringMediaAssetDecoration;
    const acquireAssetDecoration = vi.fn(async () => acquired);
    const leaseClient = {
      acquireAssetDecoration,
    } as unknown as AuthoringMediaSourceLeaseClient;

    render(
      <NleAuthoringAssetBin
        locale="en"
        authoring={authoring}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={9}
        leaseClient={leaseClient}
        currentFrame={() => 0}
        decorationDemandBroker={decorationDemandBroker}
        onIntent={vi.fn(async () => undefined)}
      />,
    );

    expect(decorationDemandBroker.register).toHaveBeenCalledWith(
      "nle-authoring-asset-bin-thumbnails",
      "thumbnail",
      expect.any(Function),
    );
    expect(activeDemands).toHaveLength(1);
    expect(activeDemands[0]?.key).toContain("workspace.1");
    await activeDemands[0]!.acquire(new AbortController().signal);

    expect(acquireAssetDecoration).toHaveBeenCalledWith(
      {
        authoring,
        manifest: buildNleAuthoringAssetManifest(authoring),
        assetId: "asset.video",
        ownerId: "nle-authoring-decoration-9-1",
        runtimeEpoch: 9,
        derivativeKind: "thumbnail",
      },
      expect.any(AbortSignal),
    );
  });

  it("inserts from the V2 catalog with the captured authoring CAS", () => {
    const onIntent = vi.fn(async (_intent: AuthoringIntent) => undefined);
    const { getByRole } = render(
      <NleAuthoringAssetBin
        locale="en"
        authoring={emptyAuthoring()}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => 0}
        onIntent={onIntent}
      />,
    );

    fireEvent.click(
      getByRole("button", { name: "Add Clip 01 to the timeline" }),
    );
    fireEvent.click(
      getByRole("button", { name: "Add Clip 01 to the timeline" }),
    );

    expect(onIntent).toHaveBeenCalledTimes(1);
    expect(onIntent).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "apply_timeline_commands",
        capturedTimeline: {
          workspaceHandle: "workspace.1",
          workspaceRevision: 3,
          timelineRevision: 4,
          timelineFingerprint: `sha256:${"2".repeat(64)}`,
          authoringFingerprint: `sha256:${"3".repeat(64)}`,
        },
      }),
    );
    const intent = onIntent.mock.calls[0]![0] as Extract<
      AuthoringIntent,
      { action: "apply_timeline_commands" }
    >;
    if (intent.action !== "apply_timeline_commands")
      throw new Error("expected a timeline command intent");
    const command = intent.commands[0]!;
    expect(command).toMatchObject({
      kind: "insert_asset_clip",
      payload: {
        clip: {
          clip_id: "clip-r4-1",
          asset_id: "asset.video",
          track_id: "track.primary",
          start_frame: 0,
          duration_frames: 48,
        },
      },
    });
  });

  it("exposes the added badge contract for accepted and imported placements", () => {
    const authoring: NleAuthoringStateV2 = {
      ...emptyAuthoring(),
      contentEndExclusive: 48,
      clips: [
        {
          clipId: "clip-existing",
          assetId: video.assetId,
          trackId: "track.primary",
          startFrame: 0,
          durationFrames: 48,
          sourceStartFrame: 0,
          enabled: true,
          transform: {},
          crop: {},
          opacityBp: 10_000,
          blend: "normal",
          text: null,
          transition: { kind: "cut", durationFrames: 0 },
          effect: {},
          audio: IDENTITY_CLIP_AUDIO,
        },
      ],
    };
    const props = {
      locale: "en" as const,
      authoring,
      busy: false,
      runtimeEpoch: 1,
      leaseClient: {} as AuthoringMediaSourceLeaseClient,
      currentFrame: () => 0,
      onIntent: vi.fn(async (_intent: AuthoringIntent) => undefined),
    };
    const { container, rerender } = render(
      <NleAuthoringAssetBin {...props} highlightedAssetIds={[]} />,
    );

    expect(
      container.querySelector('[data-h3-nle-media-badge="added"]')?.textContent,
    ).toBe("Added");

    rerender(
      <NleAuthoringAssetBin {...props} highlightedAssetIds={[video.assetId]} />,
    );
    expect(
      container.querySelector('[data-h3-nle-media-badge="added"]')?.textContent,
    ).toBe("Imported");
  });

  it("keeps the accepted range commands reachable from a V2 asset card", () => {
    const onIntent = vi.fn(async (_intent: AuthoringIntent) => undefined);
    let currentFrame = 0;
    const authoring: NleAuthoringStateV2 = {
      ...emptyAuthoring(),
      contentEndExclusive: 192,
      clips: [
        {
          clipId: "clip-existing",
          assetId: video.assetId,
          trackId: "track.primary",
          startFrame: 0,
          durationFrames: 192,
          sourceStartFrame: 0,
          enabled: true,
          transform: {},
          crop: {},
          opacityBp: 10_000,
          blend: "normal",
          text: null,
          transition: { kind: "cut", durationFrames: 0 },
          effect: {},
          audio: IDENTITY_CLIP_AUDIO,
        },
      ],
    };
    const { getByRole } = render(
      <NleAuthoringAssetBin
        locale="en"
        authoring={authoring}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => currentFrame}
        onIntent={onIntent}
      />,
    );
    const card = getByRole("button", {
      name: "Add Clip 01 to the timeline",
    });

    fireEvent.contextMenu(card);
    const insert = document.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="range.insert"]',
    );
    expect(insert).not.toBeNull();
    fireEvent.click(insert!);
    expect(onIntent.mock.calls[0]![0]).toMatchObject({
      action: "apply_timeline_commands",
      commands: [
        {
          kind: "insert_range",
          payload: {
            clip: {
              clip_id: "clip-r4-1",
              start_frame: 0,
              duration_frames: 48,
            },
            scope_track_ids: ["track.primary"],
          },
        },
      ],
    });

    currentFrame = 96;
    fireEvent.contextMenu(card);
    const overwrite = document.querySelector<HTMLButtonElement>(
      '[data-h3-nle-control="range.overwrite"]',
    );
    expect(overwrite).not.toBeNull();
    fireEvent.click(overwrite!);
    expect(onIntent.mock.calls[1]![0]).toMatchObject({
      action: "apply_timeline_commands",
      commands: [
        {
          kind: "overwrite_range",
          payload: {
            clip: {
              clip_id: "clip-r4-1",
              start_frame: 96,
              duration_frames: 48,
            },
            scope_track_ids: ["track.primary"],
            remainder_ids: { "clip-existing": "remainder-r4-1" },
          },
        },
      ],
    });
  });

  it("inserts a V2 image card on a compatible overlay at the playhead", () => {
    const onIntent = vi.fn(async (_intent: AuthoringIntent) => undefined);
    const authoring: NleAuthoringStateV2 = {
      ...emptyAuthoring(),
      contentEndExclusive: 96,
      assets: [video, image],
      tracks: [
        ...emptyAuthoring().tracks,
        {
          trackId: "track.image",
          kind: "image_overlay",
          order: 1,
          enabled: true,
          locked: false,
        },
      ],
      clips: [
        {
          clipId: "clip-existing",
          assetId: video.assetId,
          trackId: "track.primary",
          startFrame: 0,
          durationFrames: 96,
          sourceStartFrame: 0,
          enabled: true,
          transform: {},
          crop: {},
          opacityBp: 10_000,
          blend: "normal",
          text: null,
          transition: { kind: "cut", durationFrames: 0 },
          effect: {},
          audio: IDENTITY_CLIP_AUDIO,
        },
      ],
    };
    const { getByRole } = render(
      <NleAuthoringAssetBin
        locale="en"
        authoring={authoring}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => 48}
        onIntent={onIntent}
      />,
    );

    fireEvent.click(
      getByRole("button", { name: "Add Clip 02 to the timeline" }),
    );

    expect(onIntent.mock.calls[0]![0]).toMatchObject({
      action: "apply_timeline_commands",
      commands: [
        {
          kind: "insert_asset_clip",
          payload: {
            clip: {
              asset_id: "asset.image",
              track_id: "track.image",
              start_frame: 48,
              duration_frames: 1,
            },
          },
        },
      ],
    });
  });

  it("shows a typed refusal when the primary V2 track is locked", () => {
    const onIntent = vi.fn(async (_intent: AuthoringIntent) => undefined);
    const authoring = {
      ...emptyAuthoring(),
      tracks: [{ ...emptyAuthoring().tracks[0]!, locked: true }],
    };
    const { getByRole } = render(
      <NleAuthoringAssetBin
        locale="en"
        authoring={authoring}
        highlightedAssetIds={[]}
        busy={false}
        runtimeEpoch={1}
        leaseClient={{} as AuthoringMediaSourceLeaseClient}
        currentFrame={() => 0}
        onIntent={onIntent}
      />,
    );

    const add = getByRole("button", {
      name: "Add Clip 01 to the timeline",
    }) as HTMLButtonElement;
    expect(add.disabled).toBe(false);
    fireEvent.click(add);
    expect(onIntent).not.toHaveBeenCalled();
    expect(getByRole("status").textContent).toContain("unlock the primary");
  });
});
