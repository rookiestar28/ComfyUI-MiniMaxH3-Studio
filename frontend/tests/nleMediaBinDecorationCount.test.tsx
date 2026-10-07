import { act, cleanup, render, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import { NleMediaBin } from "../src/components/nle/NleMediaBin";
import { useNleFilmstrips } from "../src/components/nle/useNleFilmstrips";
import { useNleWaveforms } from "../src/components/nle/useNleWaveforms";
import {
  AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
  canonicalPublicRuntimeAssetFingerprint,
} from "../src/contracts/authoringMediaLeaseCodec";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
  type PublicCompositionSnapshot,
} from "../src/contracts/compositionCodec";
import {
  createNleDecorationDemandBroker,
  type NleRoutedDecorationValue,
} from "../src/host/nleDecorationDemandBroker";
import { createNleLeaseScheduler } from "../src/host/nleLeaseScheduler";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaDecorationLeaseClient,
  AuthoringMediaAssetDecorationContextV1,
} from "../src/host/authoringMediaSourceLease";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("withdraws cached thumbnails before other producers refresh the shared demand set", async () => {
  const wire = structuredClone(fixture.snapshot);
  const video = wire.assets.find(({ asset_id }) => asset_id === "vid-primary")!;
  video.source_frame_count = 4;
  video.landmarks = Array.from({ length: 4 }, (_, frame_index) => ({
    frame_index,
    pts: frame_index * 512,
    dts: frame_index * 512,
    duration_ticks: 512,
  }));
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  const before = decodePublicCompositionSnapshot(wire);
  const changed = structuredClone(wire);
  changed.clips[0]!.opacity_bp = 9000;
  changed.workspace_revision++;
  changed.timeline_revision++;
  changed.public_fingerprint = publicCompositionFingerprint(changed);
  const after = decodePublicCompositionSnapshot(changed);
  const creates: string[] = [];
  const releases = vi.fn(async () => undefined);
  const bitmaps: { close: ReturnType<typeof vi.fn> }[] = [];
  const leaseClient = {
    acquireAssetDecoration: vi.fn(
      async (context: AuthoringMediaAssetDecorationContextV1) => {
        const { assetId, derivativeKind } = context;
        creates.push(`${derivativeKind}:${assetId}`);
        const asset = context.manifest.assets.find(
          (member) => member.assetId === assetId,
        )!;
        const cacheKey = {
          workspaceHandle: context.snapshot.workspaceHandle,
          assetFingerprint: canonicalPublicRuntimeAssetFingerprint(asset),
          derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
          derivativeKind,
        };
        const bitmap = { width: 32, height: 18, close: vi.fn() };
        if (derivativeKind !== "audio_peaks") bitmaps.push(bitmap);
        const value =
          derivativeKind === "audio_peaks"
            ? {
                derivativeKind,
                cacheKey,
                peaks: {
                  version: 1,
                  pairRate: 100,
                  sampleRate: 48000,
                  sampleCount: 480,
                  pairCount: 1,
                  pairs: new Int8Array(2),
                  byteLength: 2,
                },
              }
            : {
                derivativeKind,
                cacheKey,
                bitmap,
                geometry: {
                  sourceWidth: 320,
                  sourceHeight: 180,
                  derivativeWidth: 32,
                  derivativeHeight: 18,
                },
                tileCount: 1,
              };
        return {
          value,
          release: releases,
          discard: () => undefined,
        } as unknown as AuthoringMediaAssetDecoration;
      },
    ),
  } satisfies Pick<
    AuthoringMediaDecorationLeaseClient,
    "acquireAssetDecoration"
  >;
  const client = leaseClient as unknown as AuthoringMediaDecorationLeaseClient;
  const scheduler =
    createNleLeaseScheduler<
      NleRoutedDecorationValue<AuthoringMediaAssetDecoration["value"]>
    >();
  const broker =
    createNleDecorationDemandBroker<AuthoringMediaAssetDecoration["value"]>(
      scheduler,
    );
  const binDemandKeys: string[][] = [];
  const observedBroker: typeof broker = {
    ...broker,
    register(sourceKey, priority, publishValue) {
      const source = broker.register(sourceKey, priority, publishValue);
      return {
        update(demands) {
          if (sourceKey === "nle-media-bin-thumbnails")
            binDemandKeys.push(demands.map(({ key }) => key));
          source.update(demands);
        },
        close: () => source.close(),
      };
    },
  };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: vi.fn(),
    clearRect: vi.fn(),
  } as unknown as CanvasRenderingContext2D);
  const visibleAssetIds = before.assets.map(({ assetId }) => assetId);
  const bin = (snapshot: PublicCompositionSnapshot) => (
    <NleMediaBin
      locale="en"
      snapshot={snapshot}
      highlightedAssetIds={[]}
      busy={false}
      runtimeEpoch={1}
      leaseClient={client}
      decorationDemandBroker={observedBroker}
      currentFrame={() => 0}
      onIntent={async () => undefined}
    />
  );
  const rendered = render(bin(before));
  const filmstrips = renderHook(
    ({ snapshot }) =>
      useNleFilmstrips({
        snapshot,
        runtimeEpoch: 1,
        leaseClient: client,
        decorationDemandBroker: observedBroker,
        visibleAssetIds,
      }),
    { initialProps: { snapshot: before } },
  );
  const waveforms = renderHook(
    ({ snapshot }) =>
      useNleWaveforms({
        snapshot,
        runtimeEpoch: 1,
        leaseClient: client,
        decorationDemandBroker: observedBroker,
        visibleAssetIds,
      }),
    { initialProps: { snapshot: before } },
  );
  const settle = async () => {
    for (
      let round = 0, previous = -1;
      round < 20 && previous !== creates.length;
      round++
    ) {
      previous = creates.length;
      await act(async () => {
        await scheduler.whenIdle();
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    }
  };
  await settle();
  const thumbnails = creates.filter((key) => key.startsWith("thumbnail:"));
  const firstSettle = [...creates];
  const firstBinPending = binDemandKeys.at(-1);
  rendered.rerender(bin(after));
  filmstrips.rerender({ snapshot: after });
  waveforms.rerender({ snapshot: after });
  await settle();
  const editCreates = creates.slice(firstSettle.length);
  rendered.unmount();
  filmstrips.unmount();
  waveforms.unmount();
  await scheduler.whenIdle();
  broker.close();
  scheduler.close();
  expect(thumbnails).toHaveLength(3);
  expect(new Set(thumbnails).size).toBe(3);
  expect(firstBinPending).toEqual([]);
  expect(
    firstSettle.filter((key) => key.startsWith("filmstrip:")),
    firstSettle.join(","),
  ).toHaveLength(2);
  expect(
    firstSettle.filter((key) => key.startsWith("audio_peaks:")),
  ).toHaveLength(1);
  expect(editCreates).toEqual([]);
  expect(releases).toHaveBeenCalledTimes(creates.length);
  expect(bitmaps.every((bitmap) => bitmap.close.mock.calls.length === 1)).toBe(
    true,
  );
});
