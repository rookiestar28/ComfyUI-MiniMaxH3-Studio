import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import { useNleFilmstrips } from "../src/components/nle/useNleFilmstrips";
import { useNleWaveforms } from "../src/components/nle/useNleWaveforms";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import type { PublicCompositionSnapshot } from "../src/contracts/compositionCodec";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaDecorationLeaseClient,
} from "../src/host/authoringMediaSourceLease";
import {
  createNleDecorationDemandBroker,
  type NleDecorationDemandBroker,
  type NleRoutedDecorationValue,
} from "../src/host/nleDecorationDemandBroker";
import { createNleLeaseScheduler } from "../src/host/nleLeaseScheduler";

afterEach(cleanup);

describe("NLE waveform demand", () => {
  it("registers the lowest-priority producer for visible primary bound audio only", async () => {
    const wire = structuredClone(fixture.snapshot);
    const video = wire.assets.find(
      ({ asset_id }) => asset_id === "vid-primary",
    )!;
    video.source_frame_count = 4;
    video.landmarks = Array.from({ length: 4 }, (_, frameIndex) => ({
      frame_index: frameIndex,
      pts: frameIndex * 512,
      dts: frameIndex * 512,
      duration_ticks: 512,
    }));
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const snapshot = decodePublicCompositionSnapshot(wire);
    const update = vi.fn();
    const close = vi.fn();
    const register = vi.fn(() => ({ update, close }));
    const broker = {
      register,
      close: vi.fn(),
    } as unknown as NleDecorationDemandBroker<
      AuthoringMediaAssetDecoration["value"]
    >;
    const acquireAssetDecoration = vi.fn();
    const leaseClient = {
      acquireAssetDecoration,
    } as unknown as AuthoringMediaDecorationLeaseClient;

    const hook = renderHook(() =>
      useNleWaveforms({
        snapshot,
        runtimeEpoch: 7,
        leaseClient,
        decorationDemandBroker: broker,
        visibleAssetIds: ["vid-primary", "vid-overlay", "img-overlay"],
      }),
    );

    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(register).toHaveBeenCalledWith(
      "nle-timeline-waveforms",
      "audio_peaks",
      expect.any(Function),
    );
    const demands = update.mock.calls.at(-1)![0] as readonly Readonly<{
      key: string;
      acquire(signal: AbortSignal): Promise<unknown>;
    }>[];
    expect(demands).toHaveLength(1);
    void demands[0]!.acquire(new AbortController().signal);
    expect(acquireAssetDecoration).toHaveBeenCalledWith(
      expect.objectContaining({
        assetId: "vid-primary",
        derivativeKind: "audio_peaks",
        ownerId: "nle-waveform-7-0",
      }),
      expect.any(AbortSignal),
    );
    expect(hook.result.current.size).toBe(0);
    hook.unmount();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it.each(["filmstrip", "waveform"] as const)(
    "keeps a visible %s acquisition when an accepted timeline revision rerenders its hook",
    async (kind) => {
      const wire = structuredClone(fixture.snapshot);
      const video = wire.assets.find(
        ({ asset_id }) => asset_id === "vid-primary",
      )!;
      video.source_frame_count = 4;
      video.landmarks = Array.from({ length: 4 }, (_, frameIndex) => ({
        frame_index: frameIndex,
        pts: frameIndex * 512,
        dts: frameIndex * 512,
        duration_ticks: 512,
      }));
      wire.public_fingerprint = publicCompositionFingerprint(wire);
      const snapshot = decodePublicCompositionSnapshot(wire);
      const nextSnapshot: PublicCompositionSnapshot = {
        ...snapshot,
        timelineRevision: snapshot.timelineRevision + 1,
      };
      const scheduler =
        createNleLeaseScheduler<
          NleRoutedDecorationValue<AuthoringMediaAssetDecoration["value"]>
        >();
      const broker =
        createNleDecorationDemandBroker<AuthoringMediaAssetDecoration["value"]>(
          scheduler,
        );
      const signals: AbortSignal[] = [];
      const acquireAssetDecoration = vi.fn(
        (
          _context: unknown,
          signal: AbortSignal,
        ): Promise<AuthoringMediaAssetDecoration> => {
          signals.push(signal);
          return new Promise((_resolve, reject) => {
            signal.addEventListener(
              "abort",
              () =>
                reject(
                  Object.assign(new Error("decoration cancelled"), {
                    disposition: "cancelled",
                  }),
                ),
              { once: true },
            );
          });
        },
      );
      const leaseClient = {
        acquireAssetDecoration,
      } as unknown as AuthoringMediaDecorationLeaseClient;
      const render = ({ value }: { value: PublicCompositionSnapshot }) => {
        const input = {
          snapshot: value,
          runtimeEpoch: 7,
          leaseClient,
          decorationDemandBroker: broker,
          visibleAssetIds: ["vid-primary"],
        };
        return kind === "filmstrip"
          ? useNleFilmstrips(input)
          : useNleWaveforms(input);
      };

      const hook = renderHook(render, { initialProps: { value: snapshot } });
      await waitFor(() => expect(signals).toHaveLength(1));
      expect(signals[0]?.aborted).toBe(false);

      hook.rerender({ value: nextSnapshot });

      expect(signals[0]?.aborted).toBe(false);
      expect(signals).toHaveLength(1);
      hook.unmount();
      broker.close();
    },
  );
});
