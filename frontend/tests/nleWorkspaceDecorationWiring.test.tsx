import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NleWorkspace } from "../src/components/nle/NleWorkspace";
import { decodeProductionAuthoringImportResponse } from "../src/contracts/productionAuthoringImportCodec";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaPreparedPlayback,
  AuthoringMediaSourceLeaseClient,
  NleAuthoringMediaAssetPreparationContext,
} from "../src/host/authoringMediaSourceLease";
import type { NleDecorationDemandBroker } from "../src/host/nleDecorationDemandBroker";
import type { NleLeaseScheduler } from "../src/host/nleLeaseScheduler";
import { importV2ResponseWire } from "./support/productionAuthoringImportFixture";
import {
  authoringReady,
  REFERENCE_SHAPE,
  SMOKE_SHAPE,
  snapshotFixture,
} from "./support/nleWorkspaceFixture";
import { PREPARATION_ASSETS } from "./support/nleAssetPreparationFixture";
import { bindingFixture, expandedState } from "./support/nleWorkspaceBinding";

const ports = vi.hoisted(() => ({
  media: {} as Record<string, unknown>,
  timeline: {} as Record<string, unknown>,
  monitor: {} as Record<string, unknown>,
}));
vi.mock("../src/host/nleLeaseScheduler", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../src/host/nleLeaseScheduler")>();
  // Keep the real scheduler; expose a mutable facade solely to observe teardown ordering.
  return {
    ...actual,
    createNleLeaseScheduler: () => ({ ...actual.createNleLeaseScheduler() }),
  };
});
vi.mock("../src/components/nle/NleMediaBin", () => ({
  NleMediaBin: (props: Record<string, unknown>) => {
    ports.media = props;
    return null;
  },
}));
vi.mock("../src/components/nle/NleTimeline", () => ({
  NleTimeline: (props: Record<string, unknown>) => {
    ports.timeline = props;
    return null;
  },
}));
vi.mock("../src/components/nle/NleMonitor", () => ({
  NleMonitor: (props: Record<string, unknown>) => {
    ports.monitor = props;
    return null;
  },
}));

afterEach(cleanup);

describe("workspace decoration ownership", () => {
  it("shares one routed broker with both decoration consumers and closes it before playback's scheduler", () => {
    const state = expandedState();
    const binding = bindingFixture({
      state: {
        ...state,
        surface: { ...state.surface, pane: "assets" },
      },
      authoring: authoringReady(SMOKE_SHAPE),
    });
    const view = render(
      <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />,
    );
    const broker = ports.media
      .decorationDemandBroker as NleDecorationDemandBroker<
      AuthoringMediaAssetDecoration["value"]
    >;
    const mediaCache = ports.media.decorationCache;
    expect(broker).toBeDefined();
    expect(mediaCache).toBeDefined();
    expect(ports.media.leaseScheduler).toBeUndefined();
    expect(ports.timeline.decorationDemandBroker).toBe(broker);
    expect(ports.timeline.leaseClient).toBe(binding.leaseClient);
    expect(ports.timeline.runtimeEpoch).toBe(state.surface.generation);
    const scheduler = ports.monitor
      .leaseScheduler as NleLeaseScheduler<unknown>;
    const source = broker.register(
      "wiring-proof",
      "filmstrip",
      () => undefined,
    );
    view.rerender(
      <NleWorkspace
        binding={{
          ...binding,
          state: {
            ...state,
            surface: { ...state.surface, pane: "text" },
          },
        }}
        onEdgeGestureActive={() => undefined}
      />,
    );
    view.rerender(
      <NleWorkspace
        binding={{
          ...binding,
          state: {
            ...state,
            surface: { ...state.surface, pane: "assets" },
          },
        }}
        onEdgeGestureActive={() => undefined}
      />,
    );
    expect(ports.media.decorationDemandBroker).toBe(broker);
    expect(ports.media.decorationCache).toBe(mediaCache);
    expect(ports.monitor.leaseScheduler).toBe(scheduler);
    const closedDuringClear: boolean[] = [];
    const update = vi.spyOn(scheduler, "updateDecorationDemand");
    update.mockImplementation(() => {
      closedDuringClear.push(scheduler.snapshot().closed);
    });
    view.unmount();
    expect(closedDuringClear).toEqual([false]);
    expect(scheduler.snapshot().closed).toBe(true);
    expect(() =>
      broker.register("late", "thumbnail", () => undefined),
    ).toThrow();
    source.close();
  });

  it("keeps timeline filmstrip and waveform ownership enabled for populated V2 authoring", () => {
    const imported = decodeProductionAuthoringImportResponse(
      importV2ResponseWire(),
    );
    if (!("historyProjection" in imported))
      throw new Error("fixture must decode as V2 authoring");
    const snapshot = snapshotFixture(REFERENCE_SHAPE);
    const authoring = {
      status: "ready" as const,
      projection: imported.authoringProjection,
      timelineHistoryV2: {
        ...imported.historyProjection,
        authoring: {
          ...imported.historyProjection.authoring,
          contentEndExclusive: Number(snapshot.output.durationFrames),
          assets: snapshot.assets,
          tracks: snapshot.tracks,
          clips: snapshot.clips,
        },
        renderSnapshot: snapshot,
      },
    };
    const binding = bindingFixture({
      authoring,
      state: expandedState({
        surface: {
          ...expandedState().surface,
          pane: "sequence",
        },
      }),
    });

    render(
      <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />,
    );

    expect(ports.timeline.decorationDemandBroker).toBeDefined();
    expect(ports.timeline.leaseClient).toBe(binding.leaseClient);
  });

  it("prepares the catalog's video assets through the workspace's own lease client and broker", async () => {
    const imported = decodeProductionAuthoringImportResponse(
      importV2ResponseWire(),
    );
    if (!("historyProjection" in imported))
      throw new Error("fixture must decode as V2 authoring");
    const signals: AbortSignal[] = [];
    const prepareAssetPlayback = vi.fn(
      (
        _context: NleAuthoringMediaAssetPreparationContext,
        signal: AbortSignal,
      ) => {
        signals.push(signal);
        // Held open, so that closing the workspace is what ends it.
        return new Promise<AuthoringMediaPreparedPlayback>(
          (_resolve, reject) => {
            signal.addEventListener(
              "abort",
              () =>
                reject(
                  Object.assign(new Error("cancelled"), {
                    disposition: "cancelled",
                  }),
                ),
              { once: true },
            );
          },
        );
      },
    );
    const authoringV2 = {
      ...imported.historyProjection.authoring,
      contentEndExclusive: 0,
      assets: PREPARATION_ASSETS,
      clips: [],
    };
    const state = expandedState({
      surface: { ...expandedState().surface, pane: "sequence", generation: 4 },
    });
    const binding = bindingFixture({
      authoring: {
        status: "ready" as const,
        projection: imported.authoringProjection,
        timelineHistoryV2: {
          ...imported.historyProjection,
          authoring: authoringV2,
          renderSnapshot: null,
        },
      },
      state,
      leaseClient: {
        prepareAssetPlayback,
      } as unknown as AuthoringMediaSourceLeaseClient,
    });

    const view = render(
      <NleWorkspace binding={binding} onEdgeGestureActive={() => undefined} />,
    );

    await waitFor(() => expect(prepareAssetPlayback).toHaveBeenCalledOnce());
    expect(prepareAssetPlayback.mock.calls[0]![0]).toMatchObject({
      authoring: authoringV2,
      assetId: "vid-audio",
      ownerId: "nle-preparation-4-1",
      runtimeEpoch: 4,
      derivativeKind: "video_proxy",
    });
    // It runs on the scheduler the monitor acquires playback through, so playback preempts it.
    const scheduler = ports.monitor
      .leaseScheduler as NleLeaseScheduler<unknown>;
    expect(scheduler.snapshot().activeKey).toMatch(
      /^nle-asset-preparation\u0000/u,
    );
    expect(signals[0]!.aborted).toBe(false);
    view.unmount();
    expect(signals[0]!.aborted).toBe(true);
  });
});
