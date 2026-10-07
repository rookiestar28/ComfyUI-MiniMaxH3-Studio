import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Profiler } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import compositionFixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";

import {
  CompositionPreview,
  type CompositionPreviewSessionFactory,
} from "../src/components/CompositionPreview";
import { decodePublicCompositionSnapshot } from "../src/contracts/compositionCodec";
import type { AuthoringMediaSourceLeaseClient } from "../src/host/authoringMediaSourceLease";
import type {
  VisualCompositionBinding,
  VisualCompositionSession,
  VisualCompositionStatus,
} from "../src/runtime/visualCompositionSession";
import type { RuntimeCapabilityDisposition } from "../src/runtime/mediaCapabilities";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import type { VisualCompositorReceipt } from "../src/runtime/visualCompositor";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function binding(): VisualCompositionBinding {
  const snapshot = decodePublicCompositionSnapshot(compositionFixture.snapshot);
  return Object.freeze({
    snapshot,
    manifest: buildPublicAssetManifest(snapshot),
    capability: {} as RuntimeCapabilityDisposition,
    resolveScene: vi.fn(),
    leaseClient: {} as AuthoringMediaSourceLeaseClient,
  });
}

function status(
  state: VisualCompositionStatus["status"] = "paused",
  blocker: VisualCompositionStatus["blocker"] = null,
): VisualCompositionStatus {
  return Object.freeze({
    status: state,
    blocker,
    browserPreviewOnly: true,
    durationFrames: 48,
  });
}

function presentation(
  source: VisualCompositionBinding,
  frame: number,
): VisualCompositorReceipt {
  return Object.freeze({
    schema: "h3.visual_compositor_receipt.v1",
    status: "presented",
    profileId: source.snapshot.profileId,
    publicFingerprint: source.snapshot.publicFingerprint,
    frame,
    generation: frame + 1,
    previewWidth: 320,
    previewHeight: 180,
    renderedLayerCount: 1,
    blocker: null,
    browserPreviewOnly: true,
  });
}

function sessionHarness(source: VisualCompositionBinding) {
  let currentStatus = status();
  let currentPresentation: VisualCompositorReceipt | null = presentation(
    source,
    0,
  );
  const listeners = new Set<() => void>();
  const frameListeners = new Set<() => void>();
  const getSnapshot = vi.fn(() => currentStatus);
  const session = {
    canRebind: vi.fn(() => false),
    open: vi.fn(async () => undefined),
    resize: vi.fn(() => undefined),
    seek: vi.fn(async (_frame: number) => undefined),
    step: vi.fn(async (_direction: -1 | 1) => undefined),
    play: vi.fn(async () => undefined),
    pause: vi.fn(async () => undefined),
    recover: vi.fn(async () => undefined),
    replace: vi.fn(async (_next: VisualCompositionBinding) => undefined),
    close: vi.fn(async () => undefined),
    previewVisualTransform: vi.fn(() => null),
    getVisualLayerGeometry: vi.fn(() => null),
    getSnapshot,
    getPresentation: vi.fn(() => currentPresentation),
    getResources: vi.fn(() => ({
      imageOwners: 0,
      fontOwners: 0,
      videoOwners: 0,
      staticLeases: 0,
      pendingOperations: 0,
      blobBytes: 0,
    })),
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    subscribeFrame(listener: () => void) {
      frameListeners.add(listener);
      return () => frameListeners.delete(listener);
    },
  } as VisualCompositionSession;
  const factory = vi.fn<CompositionPreviewSessionFactory>(() => session);
  return {
    session,
    factory,
    getSnapshot,
    publishStatus(next: VisualCompositionStatus) {
      currentStatus = next;
      for (const listener of listeners) listener();
    },
    publishFrame(frame: number | null) {
      currentPresentation = frame === null ? null : presentation(source, frame);
      for (const listener of frameListeners) listener();
    },
  };
}

describe("composition preview leaf", () => {
  it("owns one accessible canvas and delegates every transport control", async () => {
    const source = binding();
    const harness = sessionHarness(source);
    const view = render(
      <CompositionPreview binding={source} sessionFactory={harness.factory} />,
    );

    await waitFor(() => expect(harness.session.open).toHaveBeenCalledOnce());
    const canvases = view.container.querySelectorAll("canvas");
    expect(canvases).toHaveLength(1);
    expect(screen.getByRole("img", { name: "Composition preview" })).toBe(
      canvases[0],
    );
    expect(harness.factory.mock.calls[0]?.[0]).toMatchObject(source);
    expect(harness.factory.mock.calls[0]?.[0].canvas).toBe(canvases[0]);
    expect(screen.getByText(/browser preview only/i)).toBeDefined();

    await userEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(harness.session.play).toHaveBeenCalledOnce();

    act(() => harness.publishStatus(status("playing")));
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(harness.session.pause).toHaveBeenCalledOnce();

    await userEvent.click(
      screen.getByRole("button", { name: "Previous frame" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Next frame" }));
    expect(harness.session.step).toHaveBeenNthCalledWith(1, -1);
    expect(harness.session.step).toHaveBeenNthCalledWith(2, 1);

    fireEvent.change(screen.getByRole("slider", { name: "Seek frame" }), {
      target: { value: "17" },
    });
    expect(harness.session.seek).toHaveBeenCalledWith(17);

    act(() => harness.publishStatus(status("blocked", "source_unavailable")));
    expect(
      screen.getByText("Composition preview unavailable: source unavailable."),
    ).toBeDefined();
    await userEvent.click(screen.getByRole("button", { name: "Recover" }));
    expect(harness.session.recover).toHaveBeenCalledOnce();

    act(() => harness.publishStatus(status("closed", "cleanup_pending")));
    expect(
      screen.getByText("Composition preview cleanup is pending."),
    ).toBeDefined();
  });

  it("replaces only changed semantic authority and catches close rejection on unmount", async () => {
    const source = binding();
    const harness = sessionHarness(source);
    const view = render(
      <CompositionPreview binding={source} sessionFactory={harness.factory} />,
    );
    await waitFor(() => expect(harness.session.open).toHaveBeenCalledOnce());

    view.rerender(
      <CompositionPreview
        binding={{ ...source }}
        sessionFactory={harness.factory}
      />,
    );
    expect(harness.session.replace).not.toHaveBeenCalled();

    const next = Object.freeze({
      ...source,
      snapshot: Object.freeze({
        ...source.snapshot,
        timelineRevision: source.snapshot.timelineRevision + 1,
        publicFingerprint: `sha256:${"b".repeat(64)}`,
      }),
    }) satisfies VisualCompositionBinding;
    view.rerender(
      <CompositionPreview binding={next} sessionFactory={harness.factory} />,
    );
    await waitFor(() =>
      expect(harness.session.replace).toHaveBeenCalledWith(next),
    );
    expect(harness.factory).toHaveBeenCalledOnce();

    vi.mocked(harness.session.close).mockRejectedValueOnce(
      new Error("private cleanup detail"),
    );
    view.unmount();
    await act(async () => {
      await Promise.resolve();
    });
    expect(harness.session.close).toHaveBeenCalledOnce();
  });

  it("updates frame and time controls imperatively without React status renders", async () => {
    const source = binding();
    const harness = sessionHarness(source);
    const onRender = vi.fn();
    render(
      <Profiler id="composition-preview" onRender={onRender}>
        <CompositionPreview binding={source} sessionFactory={harness.factory} />
      </Profiler>,
    );
    await waitFor(() => expect(harness.session.open).toHaveBeenCalledOnce());

    const rendersBefore = onRender.mock.calls.length;
    const callsBefore = harness.getSnapshot.mock.calls.length;
    act(() => {
      for (let frame = 0; frame < 32; frame += 1) harness.publishFrame(frame);
    });

    expect(
      (
        screen.getByRole("slider", {
          name: "Seek frame",
        }) as HTMLInputElement
      ).value,
    ).toBe("31");
    expect(screen.getByText("Frame 31 at 1.292 seconds")).toBeDefined();
    expect(onRender.mock.calls.length - rendersBefore).toBeLessThanOrEqual(8);
    expect(
      harness.getSnapshot.mock.calls.length - callsBefore,
    ).toBeLessThanOrEqual(8);

    act(() => harness.publishFrame(null));
    expect(screen.getByText("Frame unavailable")).toBeDefined();
  });
});
