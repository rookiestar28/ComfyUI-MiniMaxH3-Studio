import { describe, expect, it, vi } from "vitest";
import { act, render, waitFor } from "@testing-library/react";
import { createElement, StrictMode } from "react";
import { CompositionPreview } from "../src/components/CompositionPreview";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
  RESOLVED_SCENE_SCHEMA,
  type PublicCompositionSnapshot,
} from "../src/contracts/compositionCodec";
import {
  createVisualCompositionSession,
  type VisualCompositionBinding,
} from "../src/runtime/visualCompositionSession";
import { buildPublicAssetManifest } from "../src/runtime/publicAssetManifest";
import * as visualResourceModule from "../src/runtime/visualCompositionResources";
import {
  evaluateMediaCapabilities,
  RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
  RUNTIME_PROFILE_FINGERPRINT,
  type RuntimeCapabilityDisposition,
} from "../src/runtime/mediaCapabilities";
import {
  createAuthoringLeasedEditorRuntime,
  type AuthoringLeasedEditorRuntimeOptions,
} from "../src/runtime/authoringLeasedEditorRuntime";
import {
  RUNTIME_RECEIPT_SCHEMA,
  RUNTIME_SNAPSHOT_SCHEMA,
  type EditorRuntime,
  type RuntimeReceipt,
  type RuntimeSnapshot,
} from "../src/runtime/editorRuntime";
import { runtimeContractAdmission } from "./fixtures/browserNleRuntimeFixture";

// The controller owns scheduling/fencing, while painter pixels have their own focused/browser
// coverage. Keep this boundary inert so these tests can isolate state/lifecycle race failures.
vi.mock("../src/runtime/visualCompositor", () => ({
  createVisualCompositor: (
    canvas: HTMLCanvasElement,
    snapshot: { publicFingerprint: string; profileId: string },
  ) => {
    canvas.dataset.compositors = String(
      Number(canvas.dataset.compositors ?? 0) + 1,
    );
    // The backing the stub reports is stateful, exactly as the real compositor's is: `resize`
    // adopts a new one and every later receipt reports it. A stub with a constant size cannot
    // express a repaint that is owed because the backing changed (M25-45 B-M2545-28).
    let previewWidth = 320;
    let previewHeight = 180;
    const receipt = (frame: number | null, generation: number) =>
      Object.freeze({
        schema: "h3.visual_compositor_receipt.v1",
        status: frame === null ? "unavailable" : "presented",
        profileId: snapshot.profileId,
        publicFingerprint: snapshot.publicFingerprint,
        frame,
        generation,
        previewWidth,
        previewHeight,
        renderedLayerCount: 0,
        blocker: null,
        browserPreviewOnly: true,
      });
    return {
      replace: (next: typeof snapshot) => {
        canvas.dataset.replacements = String(
          Number(canvas.dataset.replacements ?? 0) + 1,
        );
        if (canvas.dataset.rejectRebindOnce === "true") {
          delete canvas.dataset.rejectRebindOnce;
          return false;
        }
        if (canvas.dataset.rejectRebind === "true") return false;
        snapshot = next;
        return true;
      },
      resize: (box: {
        cssWidth: number;
        cssHeight: number;
        devicePixelRatio: number;
      }) => {
        previewWidth = Math.round(box.cssWidth * box.devicePixelRatio);
        previewHeight = Math.round(box.cssHeight * box.devicePixelRatio);
        return receipt(null, 0);
      },
      path: () => "native" as const,
      render: (
        scene: { frame: number },
        _resources: unknown,
        generation: number,
      ) => {
        if (canvas.dataset.context === "unavailable")
          return {
            ...receipt(null, generation),
            blocker: "canvas_unavailable",
          };
        canvas.dataset.frame = String(scene.frame);
        canvas.dataset.draftX = String(
          (
            scene as {
              layers?: readonly {
                transform: Readonly<Record<string, unknown>>;
              }[];
            }
          ).layers?.[0]?.transform.position_x_bp ?? "none",
        );
        canvas.dataset.paintTrace = [
          ...(canvas.dataset.paintTrace ?? "").split(",").filter(Boolean),
          String(scene.frame),
        ].join(",");
        return receipt(scene.frame, generation);
      },
      layerGeometry: () => null,
      clear: (generation = 0) => {
        canvas.dataset.clears = String(Number(canvas.dataset.clears ?? 0) + 1);
        canvas.dataset.frame = "";
        return receipt(null, generation);
      },
      close: () => {
        canvas.dataset.frame = "";
        return receipt(null, 0);
      },
    };
  },
}));

function qualifiedCapability(
  requestVideoFrameCallback = true,
): RuntimeCapabilityDisposition {
  const admission = runtimeContractAdmission();
  return evaluateMediaCapabilities(
    {
      schema: RUNTIME_CAPABILITY_OBSERVATION_SCHEMA,
      profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
      htmlMediaElement: true,
      canPlayMp4H264Aac: "probably",
      requestVideoFrameCallback,
      seekedEvent: true,
      timeupdateEvent: true,
      canvas2d: true,
      crossOriginIsolated: false,
    },
    admission.qualification,
    admission.authority,
  );
}

// Two primary clips meeting at frame 24, so a blocked receipt can name an owner that ends
// before the timeline does.
function adjacentCutWire() {
  const wire = structuredClone(fixture.snapshot);
  const first = wire.clips[0]!;
  first.duration_frames = 24;
  wire.clips = [
    first,
    { ...structuredClone(first), clip_id: "clip-second", start_frame: 24 },
  ];
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return wire;
}

function setup(
  canvas = document.createElement("canvas"),
  wire = structuredClone(fixture.snapshot),
  // Whether the runtime offers `prepare` (the warm owner of the next ownership change), and
  // whether the session's timer is the hand-driven one below instead of the real clock's.
  capabilities: {
    prepare?: boolean;
    timers?: boolean;
    scene?: (frame: number, snapshot: PublicCompositionSnapshot) => unknown;
  } = {},
) {
  const snapshot = decodePublicCompositionSnapshot(wire);
  let currentSnapshot = snapshot;
  const capability = qualifiedCapability();
  let milliseconds = 0;
  let callback: FrameRequestCallback | undefined;
  let factoryOptions!: AuthoringLeasedEditorRuntimeOptions;
  let abort = new AbortController();
  const runtimeListeners = new Set<() => void>();
  let runtimeState: RuntimeSnapshot = {
    schema: RUNTIME_SNAPSHOT_SCHEMA,
    status: "closed",
    epoch: 0,
    profileFingerprint: RUNTIME_PROFILE_FINGERPRINT,
    publicFingerprint: snapshot.publicFingerprint,
    outputFrame: null,
    sources: [],
    blocker: null,
    resources: {
      activeVideoOwners: 0,
      warmVideoOwners: 0,
      canvasOwners: 0,
      pendingOperations: 0,
      pendingRvfcOwners: 0,
    },
  };
  const receipt = () => ({
    schema: RUNTIME_RECEIPT_SCHEMA,
    status: "applied" as const,
    epoch: runtimeState.epoch,
    outputFrame: runtimeState.outputFrame,
    blocker: null,
  });
  async function resolve(frame: number) {
    abort.abort();
    abort = new AbortController();
    const epoch = runtimeState.epoch + 1;
    runtimeState = { ...runtimeState, epoch };
    await factoryOptions.resolveScene(frame, currentSnapshot, {
      epoch,
      signal: abort.signal,
    });
    runtimeState = { ...runtimeState, status: "paused", outputFrame: frame };
    return receipt();
  }
  const prepare = vi.fn(async (_upcoming: readonly unknown[]) => receipt());
  const timers: Array<{
    callback: () => void;
    delayMs: number;
    live: boolean;
  }> = [];
  const runtime: EditorRuntime = {
    capabilities: () => capability,
    ...(capabilities.prepare ? { prepare } : {}),
    open: vi.fn((_manifest, next) => {
      currentSnapshot = next;
      runtimeState = {
        ...runtimeState,
        publicFingerprint: next.publicFingerprint,
      };
      return resolve(0);
    }),
    replace: vi.fn((_manifest, next, target) => {
      currentSnapshot = next;
      runtimeState = {
        ...runtimeState,
        publicFingerprint: next.publicFingerprint,
      };
      return resolve(
        typeof target === "function"
          ? target()
          : (target ?? runtimeState.outputFrame ?? 0),
      );
    }),
    seek: vi.fn(resolve),
    advance: vi.fn(async (frame: number) => {
      await factoryOptions.resolveScene(frame, currentSnapshot, {
        epoch: runtimeState.epoch,
        signal: abort.signal,
      });
      runtimeState = { ...runtimeState, status: "playing", outputFrame: frame };
      return receipt();
    }),
    play: vi.fn(async () => {
      runtimeState = { ...runtimeState, status: "playing" };
      return receipt();
    }),
    pause: vi.fn(async () => {
      runtimeState = { ...runtimeState, status: "paused" };
      return receipt();
    }),
    snapshot: () => runtimeState,
    subscribe: (listener) => {
      runtimeListeners.add(listener);
      return () => {
        runtimeListeners.delete(listener);
      };
    },
    close: vi.fn(async () => {
      abort.abort();
      runtimeState = { ...runtimeState, status: "closed" };
      return { ...receipt(), status: "closed" as const };
    }),
  };
  const options = {
    snapshot,
    manifest: buildPublicAssetManifest(snapshot),
    capability,
    leaseClient: {
      create: vi.fn(),
      acquireVideoSource: vi.fn(),
      close: vi.fn(async () => undefined),
    },
    resolveScene:
      capabilities.scene ??
      ((frame: number, source: PublicCompositionSnapshot = snapshot) => ({
        schema: RESOLVED_SCENE_SCHEMA,
        profile_id: source.profileId,
        public_fingerprint: source.publicFingerprint,
        frame,
        layers: [],
        audio_span: null,
        blockers: [],
      })),
    canvas,
    runtimeFactory: (options: AuthoringLeasedEditorRuntimeOptions) => {
      factoryOptions = options;
      return runtime;
    },
    now: () => milliseconds,
    requestFrame: vi.fn((next: FrameRequestCallback) => {
      callback = next;
      return 1;
    }),
    cancelFrame: () => {
      callback = undefined;
    },
    ...(capabilities.timers
      ? {
          setTimer: (callback: () => void, delayMs: number) => {
            const row = { callback, delayMs, live: true };
            timers.push(row);
            return row;
          },
          clearTimer: (handle: unknown) => {
            (handle as { live: boolean }).live = false;
          },
        }
      : {}),
  };
  const session = createVisualCompositionSession(options);
  return {
    session,
    runtime,
    ownerOptions: () => factoryOptions,
    prepare,
    timers,
    /** A display tick at an arbitrary wall time, not only at a frame's start. */
    tickAt: (wallMilliseconds: number) => {
      milliseconds = wallMilliseconds;
      callback?.(milliseconds);
    },
    /** Let wall time pass with no display tick. */
    elapseTo: (wallMilliseconds: number) => {
      milliseconds = wallMilliseconds;
    },
    binding: options,
    tick: (frame: number) => {
      milliseconds = (frame * 1000) / 24 + 0.01;
      callback?.(milliseconds);
    },
    failTransport: (
      code:
        | "transport_failure"
        | "contract_mismatch"
        | "resource_limit" = "transport_failure",
    ) => {
      runtimeState = {
        ...runtimeState,
        status: "blocked",
        blocker: { code, subjectId: null },
      };
      for (const listener of runtimeListeners) listener();
    },
    advance: async (frame: number) => {
      milliseconds = (frame * 1000) / 24 + 0.01;
      callback?.(milliseconds);
      await vi.waitFor(() =>
        expect(session.getPresentation()?.frame).toBe(frame),
      );
    },
  };
}

function holdPlaybackAdvance(runtime: EditorRuntime) {
  let pending = false;
  let resolve!: (receipt: RuntimeReceipt) => void;
  let reject!: (error: Error) => void;
  const task = new Promise<RuntimeReceipt>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  const snapshot = runtime.snapshot;
  const pause = vi.mocked(runtime.pause).getMockImplementation()!;
  const seek = vi.mocked(runtime.seek).getMockImplementation()!;
  vi.spyOn(runtime, "snapshot").mockImplementation(() => {
    const state = snapshot();
    return {
      ...state,
      resources: { ...state.resources, pendingOperations: pending ? 1 : 0 },
    };
  });
  // The native runtime keeps its public playing status while an advance owns work.
  // Pause refuses that state; seek supersedes/drains it before its new receipt.
  vi.mocked(runtime.advance).mockImplementationOnce(() => {
    pending = true;
    return task;
  });
  vi.mocked(runtime.pause).mockImplementation(() =>
    pending
      ? Promise.resolve({
          schema: RUNTIME_RECEIPT_SCHEMA,
          status: "blocked",
          epoch: snapshot().epoch,
          outputFrame: snapshot().outputFrame,
          blocker: { code: "unsupported", subjectId: null },
        })
      : pause(),
  );
  vi.mocked(runtime.seek).mockImplementation((frame) => {
    pending = false;
    return seek(frame);
  });
  return {
    cancel: () => {
      pending = false;
      resolve({
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "cancelled",
        epoch: 1,
        outputFrame: null,
        blocker: { code: "cancelled", subjectId: null },
      });
    },
    fail: () => {
      pending = false;
      reject(new Error("transport_failure"));
    },
  };
}

describe("pause and intent fencing during a playback advance", () => {
  it("supersedes pending playing work before pausing the last presented frame", async () => {
    const { session, runtime, tick } = setup();
    await session.open();
    await session.play();
    const held = holdPlaybackAdvance(runtime);
    tick(8);
    expect(runtime.snapshot()).toMatchObject({
      status: "playing",
      resources: { pendingOperations: 1 },
    });
    await session.pause();
    held.cancel();
    await Promise.resolve();
    expect(runtime.seek).toHaveBeenCalledExactlyOnceWith(0);
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(session.getPresentation()).toMatchObject({
      frame: 0,
      status: "presented",
    });
    await session.close();
  });

  it("keeps the latest paused ruler target when obsolete advance work rejects", async () => {
    const { session, runtime, tick } = setup();
    await session.open();
    await session.play();
    const held = holdPlaybackAdvance(runtime);
    tick(8);
    const pausing = session.pause();
    const targets = [session.seek(4), session.seek(12), session.seek(18)];
    await Promise.all([pausing, ...targets]);
    held.fail();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(session.getPresentation()).toMatchObject({
      frame: 18,
      status: "presented",
    });
    await session.close();
  });

  it("does not let a superseded advance rejection erase a newer scrub", async () => {
    const { session, runtime, tick } = setup();
    await session.open();
    await session.play();
    const held = holdPlaybackAdvance(runtime);
    tick(8);
    await session.seek(12);
    held.fail();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(session.getSnapshot().blocker).toBeNull();
    expect(session.getPresentation()).toMatchObject({
      frame: 12,
      status: "presented",
    });
    await session.close();
  });

  it("still refuses actual failure of the current playback advance", async () => {
    const { session, runtime, tick } = setup();
    await session.open();
    await session.play();
    const held = holdPlaybackAdvance(runtime);
    tick(8);
    held.fail();
    await vi.waitFor(() =>
      expect(session.getSnapshot().status).toBe("blocked"),
    );
    expect(session.getPresentation()?.frame).toBeNull();
    await session.close();
  });

  it("keeps closed owners closed when an old advance rejects", async () => {
    const { session, runtime, tick } = setup();
    await session.open();
    await session.play();
    const held = holdPlaybackAdvance(runtime);
    tick(8);
    await session.close();
    held.fail();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(session.getSnapshot().status).toBe("closed");
    expect(runtime.snapshot().resources.pendingOperations).toBe(0);
  });
});

describe("M25-55 restore-before-first-paint", () => {
  it("opens directly at the restore target without committing bootstrap frame zero", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime } = setup(canvas);

    await session.open(() => 12);

    expect(runtime.open).toHaveBeenCalledOnce();
    expect(runtime.seek).toHaveBeenCalledExactlyOnceWith(12);
    expect(canvas.dataset.paintTrace).toBe("12");
    expect(session.getPresentation()).toMatchObject({ frame: 12 });
    await session.close();
  });

  it("uses the latest restore target while a replacement is opening and paints it once", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, binding } = setup(canvas);
    await session.open();
    canvas.dataset.paintTrace = "";
    let target = 18;
    let release!: () => void;
    const openGate = new Promise<void>((resolve) => (release = resolve));
    vi.mocked(runtime.open).mockImplementationOnce(async () => {
      await openGate;
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "applied",
        epoch: runtime.snapshot().epoch,
        outputFrame: 0,
        blocker: null,
      };
    });

    // A changed resolver deliberately takes the full replacement path; compatible revisions
    // now keep their runtime and are covered separately below.
    const replacing = session.replace(
      {
        ...binding,
        resolveScene: (frame, snapshot) =>
          binding.resolveScene(frame, snapshot),
      },
      () => target,
    );
    target = 24;
    release();
    await replacing;

    expect(runtime.seek).toHaveBeenLastCalledWith(24);
    expect(canvas.dataset.paintTrace).toBe("24");
    expect(session.getPresentation()).toMatchObject({ frame: 24 });
    await session.close();
  });
});

describe("retained session binding", () => {
  it("rejects the resolver of closed owners that remain reachable after cleanup refusal", async () => {
    const original = visualResourceModule.createVisualCompositionResources;
    const spy = vi
      .spyOn(visualResourceModule, "createVisualCompositionResources")
      .mockImplementationOnce((options) => {
        const owner = original(options);
        return {
          ...owner,
          close: async () => {
            throw new Error("source_unavailable");
          },
        };
      });
    let calls = 0;
    const { session, ownerOptions, binding } = setup(undefined, undefined, {
      scene: (frame, snapshot) => {
        calls += 1;
        return {
          schema: RESOLVED_SCENE_SCHEMA,
          profile_id: snapshot.profileId,
          public_fingerprint: snapshot.publicFingerprint,
          frame,
          layers: [],
          audio_span: null,
          blockers: [],
        };
      },
    });
    try {
      await session.open();
      const old = ownerOptions();
      await session.close();
      expect(session.getSnapshot().blocker).toBe("cleanup_pending");
      calls = 0;
      await expect(
        old.resolveScene(12, binding.snapshot, {
          epoch: 9,
          signal: new AbortController().signal,
        }),
      ).rejects.toThrow("cancelled");
      expect(calls).toBe(0);
    } finally {
      spy.mockRestore();
    }
  });
  it("leaves a closed surface closed when its held pause settles after cancellation", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    let finish!: () => void;
    const original = vi.mocked(runtime.pause).getMockImplementation()!;
    vi.mocked(runtime.pause).mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original();
    });
    const replacing = session.replace(nextBinding(binding));
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    const closing = session.close();
    finish();
    await Promise.all([replacing, closing]);
    expect(session.getSnapshot().status).toBe("closed");
    expect(runtime.replace).not.toHaveBeenCalled();
    await session.close();
  });

  it.each([12, null])(
    "uses the handed runtime frame %s before a first picture exists",
    async (frame) => {
      const { session, runtime, binding } = setup();
      const originalOpen = vi.mocked(runtime.open).getMockImplementation()!;
      let finish!: () => void;
      vi.mocked(runtime.open).mockImplementationOnce(async (...args) => {
        await new Promise<void>((resolve) => {
          finish = resolve;
        });
        return originalOpen(...args);
      });
      const opening = session.open();
      await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
      const state = runtime.snapshot();
      const claim = vi
        .spyOn(runtime, "snapshot")
        .mockReturnValue({ ...state, status: "paused", outputFrame: frame });
      const originalReplace = vi
        .mocked(runtime.replace)
        .getMockImplementation()!;
      vi.mocked(runtime.replace).mockImplementationOnce((...args) => {
        claim.mockRestore();
        return originalReplace(...args);
      });
      try {
        await session.pause();
        expect(session.getPresentation()).toBeNull();
        await session.replace(nextBinding(binding));
        expect(session.getSnapshot()).toMatchObject({
          status: "paused",
          blocker: null,
        });
        expect(session.getPresentation()?.frame).toBe(frame ?? 0);
      } finally {
        claim.mockRestore();
        await session.close();
        finish();
        await opening;
      }
    },
  );

  function nextBinding(binding: VisualCompositionBinding) {
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += 1;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const snapshot = decodePublicCompositionSnapshot(wire);
    return {
      ...binding,
      snapshot,
      manifest: buildPublicAssetManifest(snapshot),
      capability: qualifiedCapability(),
    };
  }

  it("does not prepare a scene delivered after close to an independently live resolver signal", async () => {
    const original = visualResourceModule.createVisualCompositionResources;
    let preparing!: ReturnType<
      typeof vi.fn<ReturnType<typeof original>["prepare"]>
    >;
    const spy = vi
      .spyOn(visualResourceModule, "createVisualCompositionResources")
      .mockImplementationOnce((options) => {
        const owner = original(options);
        preparing = vi.fn(owner.prepare);
        return { ...owner, prepare: preparing };
      });
    let hold = false;
    let finish!: (wire: unknown) => void;
    let heldWire: unknown;
    const { session, ownerOptions, binding } = setup(undefined, undefined, {
      scene: (frame, snapshot) => {
        const wire = {
          schema: RESOLVED_SCENE_SCHEMA,
          profile_id: snapshot.profileId,
          public_fingerprint: snapshot.publicFingerprint,
          frame,
          layers: [],
          audio_span: null,
          blockers: [],
        };
        if (!hold) return wire;
        heldWire = wire;
        return new Promise((resolve) => {
          finish = resolve;
        });
      },
    });
    let pending: Promise<unknown> | undefined;
    try {
      await session.open();
      preparing.mockClear();
      hold = true;
      pending = Promise.resolve(
        ownerOptions().resolveScene(12, binding.snapshot, {
          epoch: 9,
          signal: new AbortController().signal,
        }),
      );
      const refused = expect(pending).rejects.toThrow("cancelled");
      await session.close();
      finish(heldWire);
      await refused;
      expect(preparing).not.toHaveBeenCalled();
    } finally {
      finish?.(heldWire);
      await pending?.catch(() => undefined);
      spy.mockRestore();
      await session.close();
    }
  });

  it("rejects a held resource completion from owners replaced by a fresh opening", async () => {
    const original = visualResourceModule.createVisualCompositionResources;
    let hold = false;
    let finish!: () => void;
    const spy = vi
      .spyOn(visualResourceModule, "createVisualCompositionResources")
      .mockImplementationOnce((options) => {
        const owner = original(options);
        return {
          ...owner,
          prepare: async (...args: Parameters<typeof owner.prepare>) => {
            if (hold)
              await new Promise<void>((resolve) => {
                finish = resolve;
              });
            else await owner.prepare(...args);
          },
        };
      });
    const { session, ownerOptions, binding } = setup();
    let pending: Promise<unknown> | undefined;
    try {
      await session.open();
      hold = true;
      pending = Promise.resolve(
        ownerOptions().resolveScene(12, binding.snapshot, {
          epoch: 9,
          signal: new AbortController().signal,
        }),
      );
      const refused = expect(pending).rejects.toThrow("cancelled");
      await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
      await session.recover();
      finish();
      await refused;
      expect(session.getPresentation()?.frame).toBe(0);
    } finally {
      finish?.();
      await pending?.catch(() => undefined);
      spy.mockRestore();
      await session.close();
    }
  });

  it("does not ask a retired runtime to seek after its held replacement receipt arrives", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    let finish!: () => void;
    vi.mocked(runtime.replace).mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "applied",
        epoch: 2,
        outputFrame: 0,
        blocker: null,
      };
    });
    const replacing = session.replace(nextBinding(binding), 12);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    const closing = session.close();
    finish();
    await Promise.all([replacing, closing]);
    expect(runtime.seek).not.toHaveBeenCalled();
    await session.close();
  });

  it("does not paint a settled initial seek superseded by a newer revision", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, binding } = setup(canvas);
    await session.open();
    canvas.dataset.paintTrace = "";
    let target = 12;
    const originalReplace = vi.mocked(runtime.replace).getMockImplementation()!;
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      const receipt = await originalReplace(...args);
      target = 24;
      return receipt;
    });
    let finish!: () => void;
    const originalSeek = vi.mocked(runtime.seek).getMockImplementation()!;
    vi.mocked(runtime.seek).mockImplementationOnce(async (frame) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return originalSeek(frame);
    });
    const first = session.replace(nextBinding(binding), () => target);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    const next = session.replace(nextBinding(binding), 36);
    finish();
    await Promise.all([first, next]);
    expect(canvas.dataset.paintTrace).toBe("36");
    await session.close();
  });

  it("publishes the new duration before a retained runtime replacement settles", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    const wire = structuredClone(fixture.snapshot);
    wire.output.duration_frames = 60;
    wire.timeline_revision += 1;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const snapshot = decodePublicCompositionSnapshot(wire);
    let finish!: () => void;
    const original = vi.mocked(runtime.replace).getMockImplementation()!;
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original(...args);
    });
    const pending = session.replace({
      ...binding,
      snapshot,
      manifest: buildPublicAssetManifest(snapshot),
    });
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    try {
      expect(session.getSnapshot().durationFrames).toBe(60);
    } finally {
      finish();
      await pending;
      await session.close();
    }
  });

  it("does not schedule a resize for a closed surface before its first open", async () => {
    const { session, binding } = setup();
    session.resize({ cssWidth: 640, cssHeight: 360, devicePixelRatio: 1 });
    expect(binding.requestFrame).not.toHaveBeenCalled();
    await session.close();
  });

  it("rejects an old resolver before invoking the binding after owners close", async () => {
    let calls = 0;
    const { session, ownerOptions, binding } = setup(undefined, undefined, {
      scene: (frame, snapshot) => {
        calls += 1;
        return {
          schema: RESOLVED_SCENE_SCHEMA,
          profile_id: snapshot.profileId,
          public_fingerprint: snapshot.publicFingerprint,
          frame,
          layers: [],
          audio_span: null,
          blockers: [],
        };
      },
    });
    await session.open();
    const old = ownerOptions();
    await session.close();
    calls = 0;
    await expect(
      old.resolveScene(12, binding.snapshot, {
        epoch: 9,
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow("cancelled");
    expect(calls).toBe(0);
  });

  it("rejects an old resolver identity after the same session reopens", async () => {
    const { session, ownerOptions, binding } = setup();
    await session.open();
    const old = ownerOptions();
    await session.recover();
    await expect(
      old.resolveScene(12, binding.snapshot, {
        epoch: 9,
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow("cancelled");
    await session.close();
  });

  it("reports an invalid scene requested directly by a supplied runtime", async () => {
    let invalid = false;
    const { session, ownerOptions, binding } = setup(undefined, undefined, {
      scene: (frame, snapshot) =>
        invalid
          ? { schema: "invalid" }
          : {
              schema: RESOLVED_SCENE_SCHEMA,
              profile_id: snapshot.profileId,
              public_fingerprint: snapshot.publicFingerprint,
              frame,
              layers: [],
              audio_span: null,
              blockers: [],
            },
    });
    await session.open();
    invalid = true;
    await expect(
      ownerOptions().resolveScene(12, binding.snapshot, {
        epoch: 9,
        signal: new AbortController().signal,
      }),
    ).rejects.toThrow("invalid_contract");
    expect(session.getSnapshot()).toMatchObject({
      status: "blocked",
      blocker: "invalid_contract",
    });
    await session.close();
  });

  it("reports resource failure requested directly by a supplied runtime", async () => {
    const original = visualResourceModule.createVisualCompositionResources;
    let fail = false;
    const spy = vi
      .spyOn(visualResourceModule, "createVisualCompositionResources")
      .mockImplementationOnce((options) => {
        const owner = original(options);
        return {
          ...owner,
          prepare: async (...args: Parameters<typeof owner.prepare>) => {
            if (fail) throw new Error("source_unavailable");
            await owner.prepare(...args);
          },
        };
      });
    const { session, ownerOptions, binding } = setup();
    try {
      await session.open();
      fail = true;
      await expect(
        ownerOptions().resolveScene(12, binding.snapshot, {
          epoch: 9,
          signal: new AbortController().signal,
        }),
      ).rejects.toThrow("source_unavailable");
      expect(session.getSnapshot()).toMatchObject({
        status: "blocked",
        blocker: "source_unavailable",
      });
    } finally {
      spy.mockRestore();
      await session.close();
    }
  });

  it("joins a held resource replacement before asking the runtime to reauthorize", async () => {
    const original = visualResourceModule.createVisualCompositionResources;
    let finish!: () => void;
    const spy = vi
      .spyOn(visualResourceModule, "createVisualCompositionResources")
      .mockImplementationOnce((options) => {
        const owner = original(options);
        return {
          ...owner,
          replace: async (...args: Parameters<typeof owner.replace>) => {
            await new Promise<void>((resolve) => {
              finish = resolve;
            });
            await owner.replace(...args);
          },
        };
      });
    const { session, runtime, binding } = setup();
    let replacing: Promise<void> | undefined;
    try {
      await session.open();
      replacing = session.replace(nextBinding(binding));
      await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
      expect(runtime.replace).not.toHaveBeenCalled();
      finish();
      await replacing;
      expect(runtime.replace).toHaveBeenCalledOnce();
    } finally {
      finish?.();
      await replacing;
      spy.mockRestore();
      await session.close();
    }
  });

  it("does not reauthorize retired owners after a held resource replacement finishes", async () => {
    const original = visualResourceModule.createVisualCompositionResources;
    let finish!: () => void;
    const spy = vi
      .spyOn(visualResourceModule, "createVisualCompositionResources")
      .mockImplementationOnce((options) => {
        const owner = original(options);
        return {
          ...owner,
          replace: async () => {
            await new Promise<void>((resolve) => {
              finish = resolve;
            });
          },
        };
      });
    const { session, runtime, binding } = setup();
    await session.open();
    const replacing = session.replace(nextBinding(binding));
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    const closing = session.close();
    finish();
    await Promise.all([replacing, closing]);
    try {
      expect(runtime.replace).not.toHaveBeenCalled();
    } finally {
      spy.mockRestore();
      await session.close();
    }
  });

  it("honors a refused receipt even when the handed runtime already exposes new paused state", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    const original = vi.mocked(runtime.replace).getMockImplementation()!;
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => ({
      ...(await original(...args)),
      status: "blocked",
      blocker: { code: "transport_failure", subjectId: null },
    }));
    await session.replace(nextBinding(binding), 12);
    expect(session.getSnapshot()).toMatchObject({
      status: "blocked",
      blocker: "source_unavailable",
    });
    expect(session.getPresentation()?.frame).toBeNull();
    await session.close();
  });

  it("skips an intermediate revision superseded while the first rebind is held", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    vi.mocked(runtime.pause).mockClear();
    let finish!: () => void;
    const original = vi.mocked(runtime.replace).getMockImplementation()!;
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original(...args);
    });
    const first = session.replace(nextBinding(binding), 12);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    const second = session.replace(nextBinding(binding), 24);
    const third = session.replace(nextBinding(binding), 36);
    finish();
    await Promise.all([first, second, third]);
    expect(runtime.replace).toHaveBeenCalledTimes(2);
    expect(runtime.pause).toHaveBeenCalledTimes(2);
    expect(session.getPresentation()?.frame).toBe(36);
    await session.close();
  });

  it("joins the first replacement before asking the runtime to accept another", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    let finish!: () => void;
    const original = vi.mocked(runtime.replace).getMockImplementation()!;
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original(...args);
    });
    const first = session.replace(nextBinding(binding), 12);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    const second = session.replace(nextBinding(binding), 24);
    for (let step = 0; step < 24; step++) await Promise.resolve();
    try {
      expect(runtime.replace).toHaveBeenCalledOnce();
      expect(runtime.close).not.toHaveBeenCalled();
    } finally {
      finish();
      await Promise.all([first, second]);
    }
    expect(session.getPresentation()?.frame).toBe(24);
    await session.close();
  });

  it("keeps the last presented frame when replacement has no requested target", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    await session.seek(17);
    const prior = runtime.snapshot();
    const snapshotClaim = vi
      .spyOn(runtime, "snapshot")
      .mockImplementation(() => ({
        ...prior,
        outputFrame: 5,
      }));
    const target = vi.fn((...args: Parameters<EditorRuntime["replace"]>) =>
      typeof args[2] === "function" ? args[2]() : args[2],
    );
    const original = vi.mocked(runtime.replace).getMockImplementation()!;
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      expect(target(...args)).toBe(17);
      snapshotClaim.mockRestore();
      return original(...args);
    });
    try {
      await session.replace(nextBinding(binding));
      expect(target).toHaveReturnedWith(17);
      expect(session.getPresentation()?.frame).toBe(17);
      expect(session.getSnapshot().status).toBe("paused");
    } finally {
      snapshotClaim.mockRestore();
      await session.close();
    }
  });

  it("reports a refused replacement receipt even without a runtime notification", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    vi.mocked(runtime.replace).mockResolvedValueOnce({
      schema: RUNTIME_RECEIPT_SCHEMA,
      status: "blocked",
      epoch: runtime.snapshot().epoch,
      outputFrame: 0,
      blocker: { code: "transport_failure", subjectId: null },
    });
    await session.replace(nextBinding(binding));
    expect(session.getSnapshot()).toMatchObject({
      status: "blocked",
      blocker: "source_unavailable",
    });
    expect(session.getPresentation()?.frame).toBeNull();
    expect(session.canRebind(nextBinding(binding))).toBe(false);
    await session.close();
  });

  it("clears the rebind deadline once an ordinary replacement settles", async () => {
    vi.useFakeTimers();
    const { session, binding } = setup();
    try {
      await session.open();
      await session.replace(nextBinding(binding));
      expect(vi.getTimerCount()).toBe(0);
      await session.close();
    } finally {
      vi.useRealTimers();
    }
  });
  it("takes the full path after preparation misses the retained deadline", async () => {
    vi.useFakeTimers();
    const canvas = document.createElement("canvas");
    const wire = adjacentCutWire();
    const { session, runtime, prepare, tick, binding } = setup(canvas, wire, {
      prepare: true,
    });
    let finish!: () => void;
    const original = prepare.getMockImplementation()!;
    prepare.mockImplementationOnce(async (upcoming) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original(upcoming);
    });
    let replacing: Promise<void> | undefined;
    try {
      await session.open();
      await session.play();
      tick(1);
      for (let step = 0; step < 40; step++) await Promise.resolve();
      expect(finish).toBeTypeOf("function");
      const nextWire = structuredClone(wire);
      nextWire.timeline_revision += 1;
      nextWire.public_fingerprint = publicCompositionFingerprint(nextWire);
      const snapshot = decodePublicCompositionSnapshot(nextWire);
      replacing = session.replace(
        { ...binding, snapshot, manifest: buildPublicAssetManifest(snapshot) },
        1,
      );
      for (let step = 0; step < 40; step++) await Promise.resolve();
      expect(vi.getTimerCount()).toBeGreaterThan(0);
      await vi.advanceTimersByTimeAsync(5_000);
      expect(runtime.replace).not.toHaveBeenCalled();
      finish();
      await replacing;
      expect(runtime.open).toHaveBeenCalledTimes(2);
      expect(canvas.dataset.compositors).toBe("2");
      await session.close();
    } finally {
      finish?.();
      await replacing;
      await session.close();
      vi.useRealTimers();
    }
  });
  it("joins the latest initial-frame seek before painting the replacement", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, binding } = setup(canvas);
    await session.open();
    canvas.dataset.paintTrace = "";
    let target = 12;
    const originalReplace = vi.mocked(runtime.replace).getMockImplementation()!;
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      const receipt = await originalReplace(...args);
      target = 24;
      return receipt;
    });
    let finish!: () => void;
    const originalSeek = vi.mocked(runtime.seek).getMockImplementation()!;
    vi.mocked(runtime.seek).mockImplementationOnce(async (frame) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return originalSeek(frame);
    });
    let settled = false;
    const replacing = session
      .replace(nextBinding(binding), () => target)
      .then(() => {
        settled = true;
      });
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    try {
      expect(runtime.seek).toHaveBeenLastCalledWith(24);
      expect(settled).toBe(false);
      expect(canvas.dataset.paintTrace).toBe("");
    } finally {
      finish();
      await replacing;
    }
    expect(canvas.dataset.paintTrace).toBe("24");
    await session.close();
  });
  it("recomputes the boundary timer when a retained revision moves the next cut", async () => {
    const wire = adjacentCutWire();
    const { session, binding, timers, tickAt } = setup(undefined, wire, {
      timers: true,
    });
    await session.open(22);
    await session.play();
    tickAt(60);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(23));
    expect(timers.filter((row) => row.live)).toHaveLength(1);
    const nextWire = structuredClone(wire);
    nextWire.clips[0]!.duration_frames = 30;
    nextWire.clips[1]!.start_frame = 30;
    nextWire.clips[1]!.duration_frames = 18;
    nextWire.timeline_revision += 1;
    nextWire.public_fingerprint = publicCompositionFingerprint(nextWire);
    const snapshot = decodePublicCompositionSnapshot(nextWire);
    await session.replace(
      { ...binding, snapshot, manifest: buildPublicAssetManifest(snapshot) },
      23,
    );
    await session.play();
    tickAt(80);
    expect(timers.filter((row) => row.live)).toHaveLength(0);
    await session.close();
  });

  it("adopts a measurement delivered during rebind without scheduling or repainting early", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    const old = session.getPresentation()!;
    let finish!: () => void;
    const original = vi.mocked(runtime.replace).getMockImplementation()!;
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original(...args);
    });
    const replacing = session.replace(nextBinding(binding));
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    binding.requestFrame.mockClear();
    session.resize({ cssWidth: 640, cssHeight: 360, devicePixelRatio: 1 });
    try {
      expect(binding.requestFrame).not.toHaveBeenCalled();
      expect(session.getPresentation()).toBe(old);
    } finally {
      finish();
      await replacing;
    }
    expect(session.getPresentation()).toMatchObject({
      previewWidth: 640,
      previewHeight: 360,
    });
    await session.close();
  });

  it("joins a successful pause before starting reauthorization", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    let finish!: () => void;
    const original = vi.mocked(runtime.pause).getMockImplementation()!;
    vi.mocked(runtime.pause).mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original();
    });
    const replacing = session.replace(nextBinding(binding));
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    for (let step = 0; step < 12; step++) await Promise.resolve();
    expect(runtime.replace).not.toHaveBeenCalled();
    finish();
    await replacing;
    expect(runtime.replace).toHaveBeenCalledOnce();
    await session.close();
  });

  it("keeps the newest binding for recovery after a resolved pause refusal", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    const next = nextBinding(binding);
    vi.mocked(runtime.pause).mockResolvedValueOnce({
      schema: RUNTIME_RECEIPT_SCHEMA,
      status: "blocked",
      epoch: runtime.snapshot().epoch,
      outputFrame: 0,
      blocker: { code: "transport_failure", subjectId: null },
    });
    await session.replace(next);
    expect(session.getSnapshot().status).toBe("blocked");
    await session.recover();
    expect(session.getPresentation()?.publicFingerprint).toBe(
      next.snapshot.publicFingerprint,
    );
    await session.close();
  });

  it("paints only the latest of overlapping accepted revisions", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, binding } = setup(canvas);
    await session.open();
    canvas.dataset.paintTrace = "";
    let finish!: () => void;
    const original = vi.mocked(runtime.replace).getMockImplementation()!;
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original(...args);
    });
    const first = session.replace(nextBinding(binding), 12);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    const second = session.replace(nextBinding(binding), 24);
    finish();
    await Promise.all([first, second]);
    expect(canvas.dataset.paintTrace).toBe("24");
    expect(session.getPresentation()?.frame).toBe(24);
    await session.close();
  });

  it("refuses retained admission when the handed runtime is independently seeking", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    const prior = runtime.snapshot();
    vi.spyOn(runtime, "snapshot").mockReturnValue({
      ...prior,
      status: "seeking",
    });
    expect(session.canRebind(nextBinding(binding))).toBe(false);
    vi.mocked(runtime.snapshot).mockRestore();
    await session.close();
  });

  it("waits for a successful held scene and refuses its late publication after close", async () => {
    let finish!: (wire: unknown) => void;
    let held = false;
    let pendingWire: unknown;
    const { session } = setup(undefined, undefined, {
      scene: (frame, snapshot) => {
        const wire = {
          schema: RESOLVED_SCENE_SCHEMA,
          profile_id: snapshot.profileId,
          public_fingerprint: snapshot.publicFingerprint,
          frame,
          layers: [],
          audio_span: null,
          blockers: [],
        };
        if (!held) return wire;
        pendingWire = wire;
        return new Promise((resolve) => {
          finish = resolve;
        });
      },
    });
    await session.open();
    held = true;
    let settled = false;
    const seeking = session.seek(12).then(() => {
      settled = true;
    });
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    for (let step = 0; step < 12; step++) await Promise.resolve();
    expect(settled).toBe(false);
    expect(session.getPresentation()?.frame).toBe(0);
    const closing = session.close();
    finish(pendingWire);
    await seeking.catch(() => undefined);
    await closing;
    expect(session.getPresentation()).toMatchObject({
      status: "unavailable",
      frame: null,
    });
    expect(session.getSnapshot().status).toBe("closed");
  });

  it("uses the new resolver after an incompatible resolver identity replacement", async () => {
    const { session, binding } = setup();
    await session.open();
    const next = nextBinding(binding);
    const resolver = vi.fn(next.resolveScene);
    await session.replace({ ...next, resolveScene: resolver });
    expect(resolver).toHaveBeenCalled();
    expect(session.getPresentation()?.publicFingerprint).toBe(
      next.snapshot.publicFingerprint,
    );
    await session.close();
  });

  it("keeps a nonzero picture and owners while a compatible revision rebinds, then paints the latest held target once", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, binding } = setup(canvas);
    await session.open();
    await session.seek(17);
    canvas.dataset.paintTrace = "";
    const states: string[] = [];
    session.subscribe(() => states.push(session.getSnapshot().status));
    const originalReplace = vi.mocked(runtime.replace).getMockImplementation()!;
    let finish!: () => void;
    const gate = new Promise<void>((resolve) => (finish = resolve));
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      await gate;
      return originalReplace(...args);
    });
    let target = 17;
    const next = nextBinding(binding);
    const replacing = session.replace(next, () => target);
    await vi.waitFor(() => expect(runtime.replace).toHaveBeenCalledOnce());
    expect(canvas.dataset.frame).toBe("17");
    expect(session.getPresentation()?.frame).toBe(17);
    expect(runtime.close).not.toHaveBeenCalled();
    expect(runtime.open).toHaveBeenCalledOnce();
    expect(canvas.dataset.compositors).toBe("1");
    expect(canvas.dataset.clears).toBeUndefined();
    target = 24;
    finish();
    await replacing;
    expect(canvas.dataset.paintTrace).toBe("24");
    expect(canvas.dataset.clears).toBeUndefined();
    expect(session.getPresentation()).toMatchObject({
      frame: 24,
      publicFingerprint: next.snapshot.publicFingerprint,
    });
    expect(session.getSnapshot().status).toBe("paused");
    expect(states).not.toContain("opening");
    expect(states).not.toContain("closing");
    await session.close();
  });

  it("rebinds an edit of a clip's audio alone in place, opening nothing again", async () => {
    // A clip's gain, mute and fades are heard through the follower's gain node; no derivative
    // and no runtime input holds them, so their edit must take the in-place path every other
    // compatible revision takes: one runtime replace, no second open, no second compositor.
    const canvas = document.createElement("canvas");
    const { session, runtime, binding } = setup(canvas);
    await session.open();
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += 1;
    (wire.clips as Array<Record<string, unknown>>).find(
      (clip) => clip.clip_id === "clip-main",
    )!.audio = {
      gain_mb: -600,
      muted: false,
      fade_in_frames: 12,
      fade_out_frames: 12,
    };
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const snapshot = decodePublicCompositionSnapshot(wire);
    expect(
      snapshot.clips.find((clip) => clip.clipId === "clip-main")!.audio.gainMb,
    ).toBe(-600);
    const next = {
      ...binding,
      snapshot,
      manifest: buildPublicAssetManifest(snapshot),
      capability: qualifiedCapability(),
    };
    expect(session.canRebind(next)).toBe(true);
    await session.replace(next);
    expect(runtime.open).toHaveBeenCalledOnce();
    expect(runtime.replace).toHaveBeenCalledOnce();
    expect(runtime.close).not.toHaveBeenCalled();
    expect(canvas.dataset.compositors).toBe("1");
    expect(canvas.dataset.replacements).toBe("1");
    expect(session.getPresentation()?.publicFingerprint).toBe(
      snapshot.publicFingerprint,
    );
    await session.close();
  });

  it.each([
    "lease-client",
    "resolver",
    "workspace",
    "profile",
    "width",
    "height",
    "manifest-profile",
    "manifest-binding",
    "capability-brand",
    "capability-status",
    "capability-blocker",
    "capability-profile",
    "capability-engine",
    "capability-fallback",
    "capability-qualified",
    "capability-limit-count",
    "capability-limit-value",
    "frame-observer",
  ])(
    "refuses retained admission for the independently supplied %s input",
    async (field) => {
      const { session, binding } = setup();
      await session.open();
      const next: VisualCompositionBinding = nextBinding(binding);
      let candidate = next;
      const alterSnapshot = (change: Partial<PublicCompositionSnapshot>) => {
        const snapshot = { ...next.snapshot, ...change };
        candidate = {
          ...next,
          snapshot,
          manifest: buildPublicAssetManifest(snapshot),
        };
      };
      const alterCapability = (change: Record<string, unknown>) => {
        candidate = {
          ...next,
          capability: {
            ...next.capability,
            ...change,
          } as RuntimeCapabilityDisposition,
        };
      };
      if (field === "lease-client")
        candidate = { ...next, leaseClient: { ...next.leaseClient } };
      if (field === "resolver")
        candidate = {
          ...next,
          resolveScene: (...args) => next.resolveScene(...args),
        };
      if (field === "workspace")
        alterSnapshot({ workspaceHandle: "a-different-workspace" });
      if (field === "profile")
        alterSnapshot({
          profileId:
            "different-profile" as PublicCompositionSnapshot["profileId"],
        });
      if (field === "width")
        alterSnapshot({
          output: {
            ...next.snapshot.output,
            width: Number(next.snapshot.output.width) + 1,
          },
        });
      if (field === "height")
        alterSnapshot({
          output: {
            ...next.snapshot.output,
            height: Number(next.snapshot.output.height) + 1,
          },
        });
      if (field === "manifest-profile")
        candidate = {
          ...next,
          manifest: {
            ...next.manifest,
            profileFingerprint: "sha256:" + "f".repeat(64),
          } as typeof next.manifest,
        };
      if (field === "manifest-binding")
        candidate = {
          ...next,
          manifest: {
            ...next.manifest,
            timelineRevision: next.manifest.timelineRevision + 1,
          },
        };
      if (field === "capability-brand") alterCapability({});
      if (field === "capability-status")
        alterCapability({ status: "unavailable" });
      if (field === "capability-blocker")
        alterCapability({ blocker: "unsupported" });
      if (field === "capability-profile")
        alterCapability({ profileFingerprint: "sha256:" + "f".repeat(64) });
      if (field === "capability-engine")
        alterCapability({ engineProfileId: "different-engine" });
      if (field === "capability-fallback")
        alterCapability({ fallback: "different-fallback" });
      if (field === "capability-qualified")
        alterCapability({ qualified: false });
      if (field === "capability-limit-count")
        alterCapability({
          limits: { ...next.capability.limits, additional: 1 },
        });
      if (field === "capability-limit-value") {
        const key = Object.keys(
          next.capability.limits,
        )[0]! as keyof typeof next.capability.limits;
        alterCapability({
          limits: {
            ...next.capability.limits,
            [key]: next.capability.limits[key] + 1,
          },
        });
      }
      if (field === "frame-observer")
        candidate = { ...next, capability: qualifiedCapability(false) };
      try {
        expect(session.canRebind(next)).toBe(true);
        expect(session.canRebind(candidate)).toBe(false);
      } finally {
        await session.close();
      }
    },
  );

  it.each(Object.keys(qualifiedCapability().limits))(
    "refuses independently changed capability limit %s",
    async (key) => {
      const { session, binding } = setup();
      await session.open();
      const next = nextBinding(binding);
      const limits = next.capability.limits;
      const candidate = {
        ...next,
        capability: {
          ...next.capability,
          limits: { ...limits, [key]: limits[key as keyof typeof limits] + 1 },
        } as RuntimeCapabilityDisposition,
      };
      try {
        expect(session.canRebind(next)).toBe(true);
        expect(session.canRebind(candidate)).toBe(false);
      } finally {
        await session.close();
      }
    },
  );

  it("joins running preparation before reauthorizing a retained owner and leaves its picture intact", async () => {
    const canvas = document.createElement("canvas");
    const wire = adjacentCutWire();
    const { session, runtime, prepare, tick, binding } = setup(canvas, wire, {
      prepare: true,
    });
    let finish!: () => void;
    const original = prepare.getMockImplementation()!;
    prepare.mockImplementationOnce(async (upcoming) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original(upcoming);
    });
    await session.open();
    await session.play();
    tick(1);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(1));
    const nextWire = structuredClone(wire);
    nextWire.timeline_revision += 1;
    nextWire.public_fingerprint = publicCompositionFingerprint(nextWire);
    const snapshot = decodePublicCompositionSnapshot(nextWire);
    const replacing = session.replace(
      { ...binding, snapshot, manifest: buildPublicAssetManifest(snapshot) },
      1,
    );
    try {
      await new Promise((resolve) => setImmediate(resolve));
      expect(runtime.replace).not.toHaveBeenCalled();
      expect(canvas.dataset.frame).toBe("1");
      expect(canvas.dataset.clears).toBeUndefined();
    } finally {
      finish();
    }
    await replacing;
    expect(runtime.replace).toHaveBeenCalledOnce();
    expect(canvas.dataset.clears).toBeUndefined();
    expect(session.getPresentation()).toMatchObject({
      frame: 1,
      publicFingerprint: snapshot.publicFingerprint,
    });
    await session.close();
  });

  it("uses full replacement when the retained painter cannot keep its capability rung", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, binding } = setup(canvas);
    await session.open();
    await session.seek(17);
    canvas.dataset.rejectRebind = "true";
    await session.replace(nextBinding(binding), 17);
    expect(runtime.replace).not.toHaveBeenCalled();
    expect(runtime.close).toHaveBeenCalledOnce();
    expect(runtime.open).toHaveBeenCalledTimes(2);
    expect(canvas.dataset.compositors).toBe("2");
    expect(session.getPresentation()?.frame).toBe(17);
    await session.close();
  });

  it("keeps the displayed owners when a refused obsolete painter replacement is superseded", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, binding } = setup(canvas);
    await session.open();
    const original = vi.mocked(runtime.pause).getMockImplementation()!;
    let finish!: () => void;
    vi.mocked(runtime.pause).mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original();
    });
    canvas.dataset.rejectRebindOnce = "true";
    const obsolete = session.replace(nextBinding(binding));
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    finish();
    for (
      let step = 0;
      step < 100 && canvas.dataset.replacements !== "1";
      step++
    )
      await Promise.resolve();
    expect(canvas.dataset.replacements).toBe("1");
    const wire = structuredClone(fixture.snapshot);
    wire.timeline_revision += 2;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const snapshot = decodePublicCompositionSnapshot(wire);
    const newest = session.replace({
      ...binding,
      snapshot,
      manifest: buildPublicAssetManifest(snapshot),
    });
    try {
      await Promise.all([obsolete, newest]);
      expect(runtime.close).not.toHaveBeenCalled();
      expect(runtime.open).toHaveBeenCalledOnce();
      expect(canvas.dataset.compositors).toBe("1");
      expect(session.getPresentation()?.publicFingerprint).toBe(
        snapshot.publicFingerprint,
      );
    } finally {
      finish();
      await Promise.allSettled([obsolete, newest]);
      await session.close();
    }
  });

  it("holds a direct session seek until the compatible revision finishes reauthorizing", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    await session.seek(17);
    const originalReplace = vi.mocked(runtime.replace).getMockImplementation()!;
    let finish!: () => void;
    const gate = new Promise<void>((resolve) => (finish = resolve));
    vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
      await gate;
      return originalReplace(...args);
    });
    const next = nextBinding(binding);
    const replacing = session.replace(next, 17);
    await vi.waitFor(() => expect(runtime.replace).toHaveBeenCalledOnce());
    vi.mocked(runtime.seek).mockClear();
    const seeking = session.seek(24);
    try {
      await Promise.resolve();
      expect(runtime.seek).not.toHaveBeenCalled();
      expect(session.getSnapshot().status).toBe("paused");
    } finally {
      finish();
    }
    await Promise.all([replacing, seeking]);
    expect(runtime.seek).toHaveBeenCalledExactlyOnceWith(24);
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(session.getPresentation()).toMatchObject({
      frame: 24,
      publicFingerprint: next.snapshot.publicFingerprint,
    });
    await session.close();
  });

  it.each(["play", "pause", "step"] as const)(
    "holds direct %s while the compatible revision reauthorizes",
    async (operation) => {
      const { session, runtime, binding } = setup();
      await session.open();
      await session.seek(17);
      const originalReplace = vi
        .mocked(runtime.replace)
        .getMockImplementation()!;
      let finish!: () => void;
      const gate = new Promise<void>((resolve) => (finish = resolve));
      vi.mocked(runtime.replace).mockImplementationOnce(async (...args) => {
        await gate;
        return originalReplace(...args);
      });
      const replacing = session.replace(nextBinding(binding), 17);
      await vi.waitFor(() => expect(runtime.replace).toHaveBeenCalledOnce());
      vi.mocked(runtime.seek).mockClear();
      vi.mocked(runtime.pause).mockClear();
      vi.mocked(runtime.play).mockClear();
      const controlling =
        operation === "step" ? session.step(1) : session[operation]();
      try {
        await Promise.resolve();
        expect(runtime.seek).not.toHaveBeenCalled();
        expect(runtime.pause).not.toHaveBeenCalled();
        expect(runtime.play).not.toHaveBeenCalled();
      } finally {
        finish();
      }
      await Promise.all([replacing, controlling]);
      expect(session.getSnapshot()).toMatchObject({
        status: operation === "play" ? "playing" : "paused",
        blocker: null,
      });
      expect(session.getPresentation()?.frame).toBe(
        operation === "step" ? 18 : 17,
      );
      if (operation === "play") expect(runtime.play).toHaveBeenCalledOnce();
      if (operation === "pause") expect(runtime.pause).toHaveBeenCalledOnce();
      if (operation === "step")
        expect(runtime.seek).toHaveBeenCalledExactlyOnceWith(18);
      await session.close();
    },
  );

  it("blocks a refused pause during rebind and keeps the newest authority for recovery", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    await session.seek(17);
    const next = nextBinding(binding);
    vi.mocked(runtime.pause).mockRejectedValueOnce(
      new Error("source_unavailable"),
    );
    await expect(session.replace(next, 17)).resolves.toBeUndefined();
    expect(session.getSnapshot()).toMatchObject({
      status: "blocked",
      blocker: "source_unavailable",
    });
    expect(session.getPresentation()?.frame).toBeNull();
    await session.recover(17);
    expect(runtime.open).toHaveBeenLastCalledWith(
      next.manifest,
      next.snapshot,
      RUNTIME_PROFILE_FINGERPRINT,
    );
    expect(session.getPresentation()).toMatchObject({
      frame: 17,
      publicFingerprint: next.snapshot.publicFingerprint,
    });
    await session.close();
  });
});

describe("visual composition transport control", () => {
  it("refuses a transform before the supplied paused runtime has delivered its first scene", async () => {
    const h = setup();
    let finish!: () => void;
    const original = vi.mocked(h.runtime.open).getMockImplementation()!;
    vi.mocked(h.runtime.open).mockImplementationOnce(async (...args) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original(...args);
    });
    const opening = h.session.open();
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    await h.session.pause();
    expect(h.session.getSnapshot().status).toBe("paused");
    try {
      expect(() =>
        h.session.previewVisualTransform("clip-main", {
          ...fixture.snapshot.clips[0]!.transform,
        }),
      ).not.toThrow();
      expect(
        h.session.previewVisualTransform("clip-main", {
          ...fixture.snapshot.clips[0]!.transform,
        }),
      ).toBeNull();
    } finally {
      finish();
      await opening;
      await h.session.close();
    }
  });
  it.each(["playing", "rebinding", "different epoch"] as const)(
    "does not retain or paint a transform draft while %s",
    async (state) => {
      const canvas = document.createElement("canvas");
      const originalFactory =
        visualResourceModule.createVisualCompositionResources;
      const factory = vi.spyOn(
        visualResourceModule,
        "createVisualCompositionResources",
      );
      factory.mockImplementationOnce((options) => ({
        ...originalFactory(options),
        prepare: async () => undefined,
        read: () => new Map(),
      }));
      const layer = fixture.snapshot.clips[2]!;
      const h = setup(canvas, undefined, {
        scene: (frame, snapshot) => ({
          schema: RESOLVED_SCENE_SCHEMA,
          profile_id: snapshot.profileId,
          public_fingerprint: snapshot.publicFingerprint,
          frame,
          layers: [
            {
              clip_id: layer.clip_id,
              asset_id: layer.asset_id,
              track_id: layer.track_id,
              source_frame: null,
              source_pts: null,
              transition_elapsed_frames: null,
              operation_ids: [
                "SelectSourceRangeV1",
                "CropV1",
                "Transform2DV1",
                "OpacityV1",
                "BlendV1",
              ],
              transform: layer.transform,
              crop: layer.crop,
              opacity_bp: layer.opacity_bp,
              blend: layer.blend,
              text: null,
              effect: layer.effect,
            },
          ],
          audio_span: null,
          blockers: [],
        }),
      });
      let finish: (() => void) | undefined;
      let replacing: Promise<void> | undefined;
      let snapshotClaim: ReturnType<typeof vi.spyOn> | undefined;
      try {
        await h.session.open();
        factory.mockRestore();
        if (state === "playing") await h.session.play();
        if (state === "rebinding") {
          const original = vi
            .mocked(h.runtime.replace)
            .getMockImplementation()!;
          vi.mocked(h.runtime.replace).mockImplementationOnce(
            async (...args) => {
              await new Promise<void>((resolve) => {
                finish = resolve;
              });
              return original(...args);
            },
          );
          const wire = structuredClone(fixture.snapshot);
          wire.timeline_revision += 1;
          wire.public_fingerprint = publicCompositionFingerprint(wire);
          const snapshot = decodePublicCompositionSnapshot(wire);
          replacing = h.session.replace({
            ...h.binding,
            snapshot,
            manifest: buildPublicAssetManifest(snapshot),
          });
          await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
        }
        if (state === "different epoch") {
          const before = h.runtime.snapshot();
          snapshotClaim = vi
            .spyOn(h.runtime, "snapshot")
            .mockReturnValue({ ...before, epoch: before.epoch + 1 });
        }
        const beforePaint = canvas.dataset.paintTrace;
        expect(
          h.session.previewVisualTransform(layer.clip_id, {
            ...layer.transform,
            position_x_bp: layer.transform.position_x_bp + 123,
          }),
        ).toBeNull();
        expect(canvas.dataset.paintTrace).toBe(beforePaint);
        snapshotClaim?.mockRestore();
        snapshotClaim = undefined;
        if (state === "playing") await h.session.pause();
        finish?.();
        await replacing;
        h.session.resize({
          cssWidth: 640,
          cssHeight: 360,
          devicePixelRatio: 1,
        });
        h.tickAt(0);
        expect(canvas.dataset.draftX).toBe(
          String(layer.transform.position_x_bp),
        );
      } finally {
        snapshotClaim?.mockRestore();
        factory.mockRestore();
        finish?.();
        await replacing;
        await h.session.close();
      }
    },
  );
  it("refuses a transform preview without a current resolved layer", async () => {
    const canvas = document.createElement("canvas");
    const { session } = setup(canvas);
    await session.open();
    expect(session.getVisualLayerGeometry("missing")).toBeNull();
    const before = canvas.dataset.paintTrace;
    expect(
      session.previewVisualTransform("missing", {
        anchor_x_bp: 5_000,
        anchor_y_bp: 5_000,
        position_x_bp: 0,
        position_y_bp: 0,
        scale_x_bp: 10_000,
        scale_y_bp: 10_000,
        rotation_mdeg: 0,
      }),
    ).toBeNull();
    expect(canvas.dataset.paintTrace).toBe(before);
    await session.close();
  });

  it("projects only leased-runtime options through an injected real factory", async () => {
    const { binding } = setup();
    const received: string[][] = [];
    const runtimeFactory = vi.fn(
      (runtimeOptions: AuthoringLeasedEditorRuntimeOptions) => {
        received.push(Object.keys(runtimeOptions).sort());
        return createAuthoringLeasedEditorRuntime(runtimeOptions);
      },
    );
    const session = createVisualCompositionSession({
      ...binding,
      canvas: document.createElement("canvas"),
      runtimeFactory,
    });
    await session.open();
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    const replacementFactory = vi.fn(() => {
      throw new Error("replacement_factory_leaked");
    });
    await session.replace({
      snapshot: binding.snapshot,
      manifest: binding.manifest,
      capability: binding.capability,
      // Exercise option projection on the full replacement path, which still constructs owners.
      resolveScene: (...args: Parameters<typeof binding.resolveScene>) =>
        binding.resolveScene(...args),
      leaseClient: binding.leaseClient,
      runtimeFactory: replacementFactory,
      canvas: document.createElement("canvas"),
      requestFrame: vi.fn(),
    } as VisualCompositionBinding);
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(runtimeFactory).toHaveBeenCalledTimes(2);
    expect(replacementFactory).not.toHaveBeenCalled();
    expect(received).toEqual([
      [
        "capability",
        "leaseClient",
        "manifest",
        "resolveScene",
        "snapshot",
        "transportOptions",
      ],
      [
        "capability",
        "leaseClient",
        "manifest",
        "resolveScene",
        "snapshot",
        "transportOptions",
      ],
    ]);
    await session.close();
  });
  it.each([
    ["contract_mismatch", "invalid_contract"],
    ["resource_limit", "resource_limit"],
  ] as const)(
    "keeps runtime %s distinct from missing media",
    async (code, blocker) => {
      const { session, runtime, failTransport } = setup();
      await session.open();
      failTransport(code);
      expect(session.getSnapshot()).toMatchObject({
        status: "blocked",
        blocker,
      });
      expect(session.getPresentation()?.status).toBe("unavailable");
      if (code === "resource_limit") {
        await vi.waitFor(() => expect(runtime.close).toHaveBeenCalledOnce());
        expect(session.getSnapshot().blocker).toBe("resource_limit");
      }
      await session.close();
    },
  );
  it("leaves a repaint owed by a new backing to the move that is in flight", async () => {
    const { session, binding, tick } = setup();
    await session.open();
    let release: (() => void) | undefined;
    // Hold the seek inside its scene resolve: the runtime has begun a new epoch and the session's
    // resolved scene still belongs to the old one, which is the state a real composition switch is
    // in for as long as the native move takes.
    await session.replace({
      ...binding,
      resolveScene: (frame: number, ...rest: unknown[]) =>
        frame === 0
          ? (binding.resolveScene as (...args: unknown[]) => unknown)(
              frame,
              ...rest,
            )
          : new Promise((resolve) => {
              release = () =>
                resolve(
                  (binding.resolveScene as (...args: unknown[]) => unknown)(
                    frame,
                    ...rest,
                  ),
                );
            }),
    } as VisualCompositionBinding);
    expect(session.getPresentation()?.frame).toBe(0);

    const moved = session.seek(1);
    await vi.waitFor(() => expect(release).toBeDefined());
    session.resize({ cssWidth: 640, cssHeight: 360, devicePixelRatio: 2 });
    tick(0);

    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    release?.();
    await moved;
    // The repaint is owed, not dropped: the settling move presents at the adopted backing.
    expect(session.getPresentation()).toMatchObject({
      frame: 1,
      previewWidth: 1280,
      previewHeight: 720,
    });
    await session.close();
  });
  it("repaints a newly measured backing when no move is in flight", async () => {
    const { session, tick } = setup();
    await session.open();
    expect(session.getPresentation()).toMatchObject({
      frame: 0,
      previewWidth: 320,
      previewHeight: 180,
    });
    session.resize({ cssWidth: 640, cssHeight: 360, devicePixelRatio: 2 });
    tick(0);
    expect(session.getPresentation()).toMatchObject({
      frame: 0,
      previewWidth: 1280,
      previewHeight: 720,
    });
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    await session.close();
  });
  it("opens at a box measured before it was open, not at the compositor's floor", async () => {
    // B-M2545-42. The view measures its pane and calls `resize` while the session is still closed
    // -- which is the ordinary order on a supplied host, where the monitor is created before the
    // surface opens. That measurement used to be dropped at `resize`'s liveness check and nothing
    // re-applied it, so the surface opened at 320 x 180 behind a 724 x 407 picture and only a
    // later user resize repaired it. Measured on the host exactly that way.
    const { session } = setup();
    session.resize({ cssWidth: 640, cssHeight: 360, devicePixelRatio: 2 });
    await session.open();
    expect(session.getPresentation()).toMatchObject({
      frame: 0,
      previewWidth: 1280,
      previewHeight: 720,
    });
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    await session.close();
  });
  it("keeps the measurement across a replacement, so a rebuilt surface opens at it", async () => {
    // The anti-regression half: the teardown clears the coalescing slot on purpose, so the fix
    // must not live there. A replacement rebuilds the compositor and reopens, and the pane is
    // still the size it was, so the new surface opens at it without waiting for another
    // observation.
    const { session, binding } = setup();
    await session.open();
    session.resize({ cssWidth: 640, cssHeight: 360, devicePixelRatio: 2 });
    await session.replace({
      ...binding,
      resolveScene: (frame, snapshot) => binding.resolveScene(frame, snapshot),
    });
    expect(session.getPresentation()).toMatchObject({
      previewWidth: 1280,
      previewHeight: 720,
    });
    await session.close();
  });
  it("reports malformed resolved scenes as invalid contracts", async () => {
    const { session, binding } = setup();
    await session.open();
    await session.replace({
      ...binding,
      resolveScene: async () => ({ schema: "invalid" }),
    });
    expect(session.getSnapshot()).toMatchObject({
      status: "blocked",
      blocker: "invalid_contract",
    });
    expect(session.getPresentation()?.status).toBe("unavailable");
    await session.close();
  });
  it("recovers the newest replacement after an old owner refuses cleanup", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    const nextResolver = vi.fn(binding.resolveScene);
    vi.mocked(runtime.close).mockRejectedValueOnce(new Error("lease_busy"));
    await session.replace({ ...binding, resolveScene: nextResolver });
    expect(session.getSnapshot().blocker).toBe("cleanup_pending");
    expect(nextResolver).not.toHaveBeenCalled();
    await session.recover();
    expect(nextResolver).toHaveBeenCalledOnce();
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    await session.close();
  });
  it("pauses at the final frame coalesced into an earlier scheduled advance", async () => {
    const { session, runtime, tick } = setup();
    await session.open();
    await session.play();
    let finish!: () => void;
    vi.mocked(runtime.advance).mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      const state = runtime.snapshot();
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "cancelled",
        epoch: state.epoch,
        outputFrame: state.outputFrame,
        blocker: { code: "cancelled", subjectId: null },
      };
    });
    tick(1);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    const last = session.getSnapshot().durationFrames - 1;
    const pending = session.seek(last);
    finish();
    await pending;
    tick(last);
    await vi.waitFor(() => expect(session.getSnapshot().status).toBe("paused"));
    expect(session.getPresentation()?.frame).toBe(last);
    await session.close();
  });
  it("advances steady playback without per-frame seek pause or play requests", async () => {
    const { session, runtime, tick } = setup();
    await session.open();
    await session.play();
    const seekCount = vi.mocked(runtime.seek).mock.calls.length;
    const pauseCount = vi.mocked(runtime.pause).mock.calls.length;
    const playCount = vi.mocked(runtime.play).mock.calls.length;
    tick(1);
    await vi.waitFor(() =>
      expect(vi.mocked(runtime.advance)).toHaveBeenCalledWith(1),
    );
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(1));
    expect(vi.mocked(runtime.seek)).toHaveBeenCalledTimes(seekCount);
    expect(vi.mocked(runtime.pause)).toHaveBeenCalledTimes(pauseCount);
    expect(vi.mocked(runtime.play)).toHaveBeenCalledTimes(playCount);
    await session.close();
  });
  it("preserves the next frame and rebases playback after a delayed owner-boundary fallback", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, tick } = setup(canvas);
    await session.open(() => 12);
    await session.play();
    canvas.dataset.paintTrace = "";
    vi.mocked(runtime.seek).mockClear();
    vi.mocked(runtime.advance).mockImplementationOnce(async () => {
      const state = runtime.snapshot();
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "blocked",
        epoch: state.epoch,
        outputFrame: state.outputFrame,
        blocker: { code: "unsupported", subjectId: null },
      };
    });
    const seek = vi.mocked(runtime.seek).getMockImplementation()!;
    let finish!: () => void;
    vi.mocked(runtime.seek).mockImplementationOnce(async (frame) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return seek(frame);
    });

    tick(21);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    tick(45);
    finish();
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(13));
    expect(canvas.dataset.paintTrace).toBe("13");
    await vi.waitFor(() => expect(runtime.play).toHaveBeenCalledTimes(2));
    // The one-second boundary acquisition must not become a second wall-clock catch-up jump.
    tick(46);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(14));
    expect(canvas.dataset.paintTrace).toBe("13,14");
    expect(runtime.seek).toHaveBeenCalledExactlyOnceWith(13);
    await session.close();
  });
  it("moves an owner-boundary fallback to the cut when the ended owner's last frame was not presented", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, tick } = setup(canvas, adjacentCutWire());
    await session.open(() => 22);
    await session.play();
    canvas.dataset.paintTrace = "";
    vi.mocked(runtime.seek).mockClear();
    // The wall clock crosses the cut at 24 while 22 is the last frame presented: the runtime
    // names the owner that ended instead of presenting its last frame.
    vi.mocked(runtime.advance).mockImplementationOnce(async () => {
      const state = runtime.snapshot();
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "blocked",
        epoch: state.epoch,
        outputFrame: state.outputFrame,
        blocker: { code: "unsupported", subjectId: "clip-main" },
      };
    });

    tick(2);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(24));
    await vi.waitFor(() => expect(runtime.play).toHaveBeenCalledTimes(2));
    // One ownership-changing seek: frame 23 belongs to the owner that already ended, and a
    // seek there is what the audio follower cancels scheduled audio for.
    expect(runtime.seek).toHaveBeenCalledExactlyOnceWith(24);
    expect(canvas.dataset.paintTrace).toBe("24");
    tick(3);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(25));
    expect(canvas.dataset.paintTrace).toBe("24,25");
    expect(runtime.seek).toHaveBeenCalledExactlyOnceWith(24);
    await session.close();
  });
  it("keeps the next-frame fallback while the wall clock is short of the named owner's end", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, tick } = setup(canvas);
    await session.open(() => 12);
    await session.play();
    canvas.dataset.paintTrace = "";
    vi.mocked(runtime.seek).mockClear();
    // `clip-main` ends with the timeline at 48: there is no cut to move to, and a receipt that
    // names an owner mid-interval must not skip the rest of that owner.
    vi.mocked(runtime.advance).mockImplementationOnce(async () => {
      const state = runtime.snapshot();
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "blocked",
        epoch: state.epoch,
        outputFrame: state.outputFrame,
        blocker: { code: "unsupported", subjectId: "clip-main" },
      };
    });

    tick(21);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(13));
    expect(runtime.seek).toHaveBeenCalledExactlyOnceWith(13);
    expect(canvas.dataset.paintTrace).toBe("13");
    expect(session.getSnapshot().status).toBe("playing");
    await session.close();
  });
  it("asks the runtime to prepare the next ownership change once per change and epoch", async () => {
    const { session, prepare, tick } = setup(undefined, adjacentCutWire(), {
      prepare: true,
    });
    await session.open();
    // Paused: nothing is about to change.
    expect(prepare).not.toHaveBeenCalled();
    await session.play();
    tick(1);
    await vi.waitFor(() => expect(prepare).toHaveBeenCalledTimes(1));
    // The scene of the cut, from the binding's own resolver.
    expect(prepare.mock.calls[0]![0]).toEqual([
      expect.objectContaining({ frame: 24 }),
    ]);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(1));
    tick(2);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(2));
    tick(3);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(3));
    expect(prepare).toHaveBeenCalledTimes(1);

    // A move begins a new runtime epoch: the same change is asked for again, once.
    await session.seek(5);
    tick(4);
    await vi.waitFor(() => expect(prepare).toHaveBeenCalledTimes(2));
    expect(prepare.mock.calls[1]![0]).toEqual([
      expect.objectContaining({ frame: 24 }),
    ]);
    tick(5);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(7));
    expect(prepare).toHaveBeenCalledTimes(2);
    await session.close();
  });
  it("does not ask when the frames up to the cut leave no lease headroom", async () => {
    const wire = adjacentCutWire();
    // An image and a title across the cut: four leases held, and the incoming clip needs two.
    wire.clips.push(structuredClone(fixture.snapshot.clips[2]!), {
      ...structuredClone(fixture.snapshot.clips[3]!),
      start_frame: 0,
      duration_frames: 48,
    });
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    const { session, prepare, tick } = setup(undefined, wire, {
      prepare: true,
    });
    await session.open();
    await session.play();
    for (const frame of [1, 2, 3]) {
      tick(frame);
      await vi.waitFor(() =>
        expect(session.getPresentation()?.frame).toBe(frame),
      );
    }
    expect(prepare).not.toHaveBeenCalled();
    await session.close();
  });
  it("gives the prepared owner back on a user pause, not on the boundary move", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, prepare, tick } = setup(
      canvas,
      adjacentCutWire(),
      { prepare: true },
    );
    await session.open(() => 22);
    await session.play();
    vi.mocked(runtime.advance).mockImplementationOnce(async () => {
      const state = runtime.snapshot();
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "blocked",
        epoch: state.epoch,
        outputFrame: state.outputFrame,
        blocker: { code: "unsupported", subjectId: "clip-main" },
      };
    });

    tick(2);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(24));
    await vi.waitFor(() => expect(runtime.play).toHaveBeenCalledTimes(2));
    // The boundary move pauses and plays the runtime; that is not the user stopping playback.
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(prepare.mock.calls[0]![0]).toHaveLength(1);

    await session.pause();
    await vi.waitFor(() => expect(prepare).toHaveBeenLastCalledWith([]));
    expect(prepare).toHaveBeenCalledTimes(2);
    await session.close();
  });
  it("lets a preparation in flight finish before it closes the owners", async () => {
    const { session, runtime, prepare, tick } = setup(
      undefined,
      adjacentCutWire(),
      { prepare: true },
    );
    let finish!: () => void;
    const settled = prepare.getMockImplementation()!;
    prepare.mockImplementationOnce(async (upcoming) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return settled(upcoming);
    });
    await session.open();
    await session.play();
    tick(1);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));

    const closing = session.close();
    await new Promise((resolve) => setImmediate(resolve));
    // The acquisition is still on the lease route: the owners are not closed under it.
    expect(runtime.close).not.toHaveBeenCalled();
    finish();
    await closing;
    expect(runtime.close).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot().status).toBe("closed");
  });
  it("starts the boundary move at the cut's own wall time, without waiting for a display tick", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, timers, tickAt, elapseTo } = setup(
      canvas,
      adjacentCutWire(),
      { timers: true },
    );
    await session.open(() => 22);
    await session.play();
    // A display tick 60 ms into playback: frame 23 is due, and the cut at frame 24 (83.3 ms)
    // is 23.3 ms away -- inside the window, so its own step is armed for that time.
    tickAt(60);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(23));
    expect(timers).toHaveLength(1);
    expect(timers[0]).toMatchObject({ delayMs: 24, live: true });

    vi.mocked(runtime.seek).mockClear();
    vi.mocked(runtime.advance).mockImplementationOnce(async () => {
      const state = runtime.snapshot();
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "blocked",
        epoch: state.epoch,
        outputFrame: state.outputFrame,
        blocker: { code: "unsupported", subjectId: "clip-main" },
      };
    });
    // The cut's wall time arrives. No display tick does: the armed step alone runs.
    elapseTo(84);
    timers[0]!.callback();
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(24));
    expect(runtime.seek).toHaveBeenCalledExactlyOnceWith(24);
    expect(session.getSnapshot().status).toBe("playing");
    await session.close();
  });
  it("arms no step far from a change and clears an armed one when playback stops", async () => {
    const { session, timers, tickAt } = setup(undefined, adjacentCutWire(), {
      timers: true,
    });
    await session.open();
    await session.play();
    // The cut at frame 24 is a second away.
    tickAt(41.7);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(1));
    expect(timers).toHaveLength(0);

    // 970 ms in, it is 30 ms away.
    tickAt(970);
    await vi.waitFor(() => expect(timers).toHaveLength(1));
    expect(timers[0]!.live).toBe(true);
    // A second tick inside the window arms nothing more.
    tickAt(985);
    expect(timers).toHaveLength(1);

    await session.pause();
    expect(timers[0]!.live).toBe(false);
    await session.close();
  });
  it("does not repaint an unchanged frame when native time has not advanced", async () => {
    const canvas = document.createElement("canvas");
    const { session, runtime, tick } = setup(canvas);
    await session.open();
    await session.play();
    canvas.dataset.paintTrace = "";
    vi.mocked(runtime.advance).mockImplementationOnce(async () => {
      const state = runtime.snapshot();
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "applied",
        epoch: state.epoch,
        outputFrame: state.outputFrame,
        blocker: null,
      };
    });
    tick(1);
    await vi.waitFor(() => expect(runtime.advance).toHaveBeenCalledWith(1));
    await Promise.resolve();
    expect(canvas.dataset.paintTrace).toBe("");
    await session.close();
  });
  it("does not resume native playback after presenting the scheduled final frame", async () => {
    const { session, runtime, tick } = setup();
    const originalPlay = vi.mocked(runtime.play).getMockImplementation()!;
    vi.mocked(runtime.play).mockImplementation(async () => {
      if (
        runtime.snapshot().outputFrame ===
        session.getSnapshot().durationFrames - 1
      )
        throw new Error("endpoint_play_failed");
      return originalPlay();
    });
    await session.open();
    await session.play();
    const last = session.getSnapshot().durationFrames - 1;
    tick(last);
    await vi.waitFor(() => expect(session.getPresentation()?.frame).toBe(last));
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(runtime.play).toHaveBeenCalledOnce();
    await session.close();
  });
  it("does not start native playback while already paused at the final frame", async () => {
    const { session, runtime } = setup();
    await session.open();
    const last = session.getSnapshot().durationFrames - 1;
    await session.seek(last);
    vi.mocked(runtime.play).mockClear();
    vi.mocked(runtime.play).mockRejectedValue(
      new Error("endpoint_play_failed"),
    );
    await session.play();
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(session.getPresentation()?.frame).toBe(last);
    expect(runtime.play).not.toHaveBeenCalled();
    await session.close();
  });
  it("reports unavailable Canvas separately from missing media and recovers explicitly", async () => {
    const canvas = document.createElement("canvas");
    canvas.dataset.context = "unavailable";
    const { session } = setup(canvas);
    await session.open();
    expect(session.getSnapshot()).toMatchObject({
      status: "blocked",
      blocker: "canvas_unavailable",
    });
    delete canvas.dataset.context;
    await session.recover();
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    await session.close();
  });
  it("protects a new StrictMode session canvas from delayed obsolete teardown", async () => {
    const created: ReturnType<typeof setup>[] = [];
    let finish!: () => void;
    const view = render(
      createElement(
        StrictMode,
        {},
        createElement(CompositionPreview, {
          binding: setup().binding,
          sessionFactory: (options) => {
            const instance = setup(options.canvas);
            if (created.length === 0)
              vi.mocked(instance.runtime.close).mockImplementationOnce(
                async () => {
                  await new Promise<void>((resolve) => {
                    finish = resolve;
                  });
                  return {
                    schema: RUNTIME_RECEIPT_SCHEMA,
                    status: "closed",
                    epoch: 1,
                    outputFrame: null,
                    blocker: null,
                  };
                },
              );
            created.push(instance);
            return instance.session;
          },
        }),
      ),
    );
    await waitFor(() =>
      expect(created[1]?.session.getPresentation()?.status).toBe("presented"),
    );
    await act(async () => {
      finish();
      await Promise.resolve();
    });
    expect(view.container.querySelector("canvas")!.dataset.frame).toBe("0");
    view.unmount();
  });
  it("does not republish a delayed seek after a newer play failure blocks presentation", async () => {
    const { session, runtime } = setup();
    await session.open();
    const seek = runtime.seek;
    let finish!: () => void;
    const original = vi.mocked(seek).getMockImplementation()!;
    vi.mocked(seek).mockImplementationOnce(async (frame) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return original(frame);
    });
    const pending = session.seek(12);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    vi.mocked(runtime.play).mockResolvedValueOnce({
      schema: RUNTIME_RECEIPT_SCHEMA,
      status: "blocked",
      epoch: 1,
      outputFrame: 0,
      blocker: { code: "transport_failure", subjectId: null },
    });
    await session.play();
    finish();
    await pending;
    expect(session.getSnapshot().status).toBe("blocked");
    expect(session.getPresentation()?.status).toBe("unavailable");
    await session.close();
  });
  it("retries a transient release failure during leaf unmount without losing its owner", async () => {
    const instance = setup();
    const view = render(
      createElement(CompositionPreview, {
        binding: instance.binding,
        sessionFactory: () => instance.session,
      }),
    );
    await waitFor(() =>
      expect(instance.session.getSnapshot().status).toBe("paused"),
    );
    vi.mocked(instance.runtime.close).mockRejectedValueOnce(
      new Error("lease_busy"),
    );
    view.unmount();
    await waitFor(() =>
      expect(instance.runtime.close).toHaveBeenCalledTimes(2),
    );
    expect(instance.session.getSnapshot().blocker).toBeNull();
  });
  it("coalesces overlapping replacements behind one close and opens only the latest binding", async () => {
    const { session, runtime, binding } = setup();
    await session.open();
    let finish!: () => void;
    const originalClose = runtime.close;
    vi.mocked(runtime.close).mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return {
        schema: RUNTIME_RECEIPT_SCHEMA,
        status: "closed",
        epoch: 1,
        outputFrame: null,
        blocker: null,
      };
    });
    const oldResolver = vi.fn(binding.resolveScene);
    const latestResolver = vi.fn(binding.resolveScene);
    const first = session.replace({ ...binding, resolveScene: oldResolver });
    const latest = session.replace({
      ...binding,
      resolveScene: latestResolver,
    });
    finish();
    await Promise.all([first, latest]);
    expect(originalClose).toHaveBeenCalledOnce();
    expect(oldResolver).not.toHaveBeenCalled();
    expect(latestResolver).toHaveBeenCalledOnce();
    await session.close();
  });
  it("invalidates a paused frame immediately when its native transport fails", async () => {
    const { session, failTransport } = setup();
    await session.open();
    failTransport();
    expect(session.getSnapshot()).toMatchObject({
      status: "blocked",
      blocker: "source_unavailable",
    });
    expect(session.getPresentation()?.frame).toBeNull();
    await session.close();
    failTransport();
    expect(session.getSnapshot().status).toBe("closed");
  });
  it("opens, seeks and steps through runtime scenes then releases on close", async () => {
    const { session, runtime } = setup();
    await session.open();
    expect(session.getSnapshot().status).toBe("paused");
    expect(session.getPresentation()?.frame).toBe(0);
    await session.seek(12);
    expect(session.getPresentation()?.frame).toBe(12);
    await session.step(-1);
    expect(session.getPresentation()?.frame).toBe(11);
    await session.close();
    expect(session.getSnapshot()).toMatchObject({
      status: "closed",
      blocker: null,
    });
    expect(session.getResources()).toMatchObject({
      imageOwners: 0,
      videoOwners: 0,
      staticLeases: 0,
    });
    expect(runtime.close).toHaveBeenCalledOnce();
  });

  it("paces output frames without publishing controls on the frame hot path", async () => {
    const { session, advance } = setup();
    const controls = vi.fn();
    const frames = vi.fn();
    session.subscribe(controls);
    session.subscribeFrame(frames);
    await session.open();
    await session.play();
    controls.mockClear();
    frames.mockClear();
    for (let frame = 1; frame <= 32; frame++) await advance(frame);
    expect(controls).not.toHaveBeenCalled();
    expect(frames).toHaveBeenCalledTimes(32);
    await session.pause();
    expect(session.getSnapshot().status).toBe("paused");
    await session.close();
  });

  it("rejects out-of-range requests without presenting an invented frame", async () => {
    const { session } = setup();
    await session.open();
    await session.seek(-1);
    expect(session.getSnapshot()).toMatchObject({
      status: "blocked",
      blocker: "invalid_contract",
    });
    expect(session.getPresentation()?.frame).toBeNull();
    await session.recover();
    expect(session.getSnapshot()).toMatchObject({
      status: "paused",
      blocker: null,
    });
    expect(session.getPresentation()?.frame).toBe(0);
    await session.close();
  });
});
