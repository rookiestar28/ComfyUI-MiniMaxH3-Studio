import {
  decodeResolvedScene,
  type PublicCompositionSnapshot,
  type ResolvedCompositionScene,
} from "../contracts/compositionCodec";
import type { AuthoringMediaSourceLeaseClient } from "../host/authoringMediaSourceLease";
import type { NlePlaybackLeaseScheduler } from "../host/nleLeaseScheduler";
import {
  createAuthoringLeasedEditorRuntime,
  type AuthoringLeasedEditorRuntimeOptions,
} from "./authoringLeasedEditorRuntime";
import type { EditorRuntime, RuntimeSceneResolver } from "./editorRuntime";
import {
  RUNTIME_PROFILE_FINGERPRINT,
  isEvaluatedAvailableDisposition,
  type RuntimeCapabilityDisposition,
} from "./mediaCapabilities";
import { nextVideoOwnerChange, planPreviewLookahead } from "./previewLookahead";
import {
  validatePublicAssetManifest,
  type PublicAssetManifest,
} from "./publicAssetManifest";
import { createVisualCompositionResources } from "./visualCompositionResources";
import {
  createVisualCompositor,
  type PreviewBox,
  type PreviewPath,
  type VisualCompositorReceipt,
  type VisualLayerGeometry,
} from "./visualCompositor";

export type VisualCompositionBinding = Readonly<{
  snapshot: PublicCompositionSnapshot;
  manifest: PublicAssetManifest;
  capability: RuntimeCapabilityDisposition;
  resolveScene: RuntimeSceneResolver;
  leaseClient: AuthoringMediaSourceLeaseClient;
}>;
export type VisualCompositionStatus = Readonly<{
  status: "closed" | "opening" | "paused" | "playing" | "blocked" | "closing";
  blocker:
    | "source_unavailable"
    | "canvas_unavailable"
    | "invalid_contract"
    | "resource_limit"
    | "cleanup_pending"
    | null;
  browserPreviewOnly: true;
  durationFrames: number;
}>;
type Options = VisualCompositionBinding &
  Readonly<{
    canvas: HTMLCanvasElement;
    playbackScheduler?: NlePlaybackLeaseScheduler;
    runtimeFactory?: (
      options: AuthoringLeasedEditorRuntimeOptions,
    ) => EditorRuntime;
    requestFrame?: (callback: FrameRequestCallback) => number;
    cancelFrame?: (handle: number) => void;
    now?: () => number;
    /** @internal The timer the step at an ownership change is armed on; a test drives it. */
    setTimer?: (callback: () => void, delayMs: number) => unknown;
    clearTimer?: (handle: unknown) => void;
    /** @internal One sample per presented frame; see `PresentationMeasurement`. */
    onPresentationMeasurement?: (sample: PresentationMeasurement) => void;
  }>;

/**
 * What one presented frame cost, from the compositor call to the frame the viewer sees.
 *
 * AC45-04 is about the whole compositor, not one helper: `paintMs` alone is the synchronous draw,
 * which stays around a millisecond even when the frame reaches the screen late, so a budget closed
 * on it would say nothing about what a viewer waits for. `deliveredMs` spans the same call through
 * to the first animation frame after it, which is the earliest moment the pixels can be on screen.
 *
 * IMPORTANT: the extra animation-frame request exists only while an observer is installed. Leaving
 * it armed in production would double the callbacks the playback loop schedules per painted frame.
 */
export type PresentationMeasurement = Readonly<{
  paintMs: number;
  deliveredMs: number;
  path: PreviewPath;
  backingWidth: number;
  backingHeight: number;
  renderedLayerCount: number;
  /**
   * Which frame of which composition this delivered paint actually was, copied from the painter's
   * own receipt.
   *
   * GUARD (D45-02): these three fields are the only post-paint evidence of WHAT was presented, and
   * an observer that reports cost without identity cannot be waited on. Without them a harness has
   * to guess that a seek has settled -- the accepted semantic sweep guessed from two equal seek
   * input values (`NleMonitor` shows `requestedFrame` before a seek settles, so they are equal
   * immediately) and two equal signatures of the first 400 RGBA values (a stale or blank frame has
   * an unchanged corner too), and 55 rows were then priced on frames that had not been presented.
   * They are copied from a receipt that already exists and change no behaviour; keep them read-only
   * and keep emitting them from inside the delivered-frame callback below, never at render time.
   */
  frame: number | null;
  generation: number;
  publicFingerprint: string;
}>;

export type VisualCompositionSession = ReturnType<
  typeof createVisualCompositionSession
>;
type InitialFrame = number | (() => number);

// IMPORTANT: React StrictMode can mount a replacement while the prior async release is pending.
// Only the current token may paint/clear this surface; an obsolete close must not erase new pixels.
const canvasOwners = new WeakMap<HTMLCanvasElement, symbol>();
// How long a release waits for a preparation in flight before it closes the owners under it.
const PREPARATION_SETTLE_DEADLINE_MS = 5_000;
// How close an ownership change must be before its own step is armed: the last display ticks
// before it (two at 60 Hz, one at 30 Hz), so no long timer outlives a rebased clock.
const BOUNDARY_STEP_WINDOW_MS = 40;
class CanvasPresentationUnavailable extends Error {}

function projectBinding(
  value: VisualCompositionBinding,
): VisualCompositionBinding {
  // IMPORTANT: session options and the leased runtime both define `runtimeFactory` at different
  // layers. Retaining or spreading the wider object can recursively invoke the outer factory.
  return Object.freeze({
    snapshot: value.snapshot,
    manifest: value.manifest,
    capability: value.capability,
    resolveScene: value.resolveScene,
    leaseClient: value.leaseClient,
  });
}

function failureDisposition(
  error: unknown,
): VisualCompositionStatus["blocker"] {
  if (error instanceof CanvasPresentationUnavailable)
    return "canvas_unavailable";
  if (error instanceof Error && error.message === "resource_limit")
    return "resource_limit";
  if (error instanceof Error && error.message === "invalid_contract")
    return "invalid_contract";
  return "source_unavailable";
}

function runtimeDisposition(
  code: string | undefined,
): VisualCompositionStatus["blocker"] {
  if (code === "contract_mismatch") return "invalid_contract";
  if (code === "resource_limit") return "resource_limit";
  return "source_unavailable";
}

export function createVisualCompositionSession(options: Options) {
  const canvasOwner = Symbol();
  canvasOwners.set(options.canvas, canvasOwner);
  const ownsCanvas = () => canvasOwners.get(options.canvas) === canvasOwner;
  const requestFrame = options.requestFrame ?? requestAnimationFrame;
  const cancelFrame = options.cancelFrame ?? cancelAnimationFrame;
  const now = options.now ?? (() => performance.now());
  const setTimer =
    options.setTimer ??
    ((callback: () => void, delayMs: number) => setTimeout(callback, delayMs));
  const clearTimer =
    options.clearTimer ??
    ((handle: unknown) =>
      clearTimeout(handle as ReturnType<typeof setTimeout>));
  let binding = projectBinding(options);
  let value: VisualCompositionStatus = Object.freeze({
    status: "closed",
    blocker: null,
    browserPreviewOnly: true,
    durationFrames: Number(binding.snapshot.output.durationFrames),
  });
  let painter = createVisualCompositor(options.canvas, binding.snapshot);
  // Outstanding measurement frames, cancelled with the session so a closed surface reports nothing.
  const presentationFrames = new Set<number>();
  let presentation: VisualCompositorReceipt | null = null;
  let scene: { value: ResolvedCompositionScene; epoch: number } | undefined;
  let visualTransformDraft: Readonly<{
    clipId: string;
    transform: Readonly<Record<string, number>>;
  }> | null = null;
  let generation = 0;
  let playing = false;
  let startTime = 0;
  let startFrame = 0;
  let scheduled: number | undefined;
  let scheduledResize: number | undefined;
  let pendingBox: PreviewBox | null = null;
  /**
   * The last box the view measured, which is a fact about the pane and not about the surface.
   *
   * GUARD (B-M2545-42): this is deliberately NOT `pendingBox`. `pendingBox` is the coalescing slot
   * for one scheduled frame and the teardown clears it, correctly -- a queued repaint outlives its
   * reason. A measurement does not: the pane is still that size after a close, a replacement or a
   * failure, so `open` adopts this on the way up and the surface never opens at the legacy floor
   * behind a large picture. Measured on the supplied host before this existed: a 724 x 407 picture
   * over a 320 x 180 backing, because `resize` was called with the real box while the session was
   * still `closed`, dropped it at the liveness check, and nothing re-applied it until a user
   * dragged a splitter. Do not merge the two variables and do not clear this in the teardown.
   */
  let measuredBox: PreviewBox | null = null;
  let scheduledMove: Promise<boolean> | undefined;
  // Where the lookahead was last planned from, the change it found, and the request in flight.
  let lookahead:
    | Readonly<{ frame: number; epoch: number; target: number | null }>
    | undefined;
  let preparing: Promise<void> | undefined;
  // The next frame at which the video owners change, read from the snapshot for the frames
  // `from` up to it, and the step armed for that frame's wall time.
  let nextChange: Readonly<{ from: number; frame: number | null }> | undefined;
  let boundaryStep: unknown;
  let manualMoves = 0;
  const pendingMoves = new Set<Promise<boolean>>();
  let requestIntent = 0;
  let closing: Promise<boolean> | undefined;
  let lifecycleIntent = 0;
  let rebinding: Promise<boolean> | undefined;
  let runtime: EditorRuntime | undefined;
  let unsubscribeRuntime: (() => void) | undefined;
  let resources:
    ReturnType<typeof createVisualCompositionResources> | undefined;
  let closed = true;
  let contextLost = false;
  const listeners = new Set<() => void>();
  const frameListeners = new Set<() => void>();
  const notify = (targets: Set<() => void>) => {
    for (const listener of targets) {
      try {
        listener();
      } catch {
        /* Observers do not own transport or cleanup. */
      }
    }
  };
  function publish(
    status: VisualCompositionStatus["status"],
    blocker: VisualCompositionStatus["blocker"] = null,
  ) {
    if (
      value.status === status &&
      value.blocker === blocker &&
      value.durationFrames === Number(binding.snapshot.output.durationFrames)
    )
      return;
    value = Object.freeze({
      status,
      blocker,
      browserPreviewOnly: true,
      durationFrames: Number(binding.snapshot.output.durationFrames),
    });
    notify(listeners);
  }
  function stopScheduling() {
    playing = false;
    if (scheduled !== undefined) cancelFrame(scheduled);
    scheduled = undefined;
    // A queued resize outlives its reason: whatever stopped the schedule -- a failure, a close, a
    // replacement -- has already decided what the surface shows.
    if (scheduledResize !== undefined) cancelFrame(scheduledResize);
    scheduledResize = undefined;
    pendingBox = null;
    if (boundaryStep !== undefined) clearTimer(boundaryStep);
    boundaryStep = undefined;
    // A measurement frame for a surface that has stopped presenting would time the next thing to
    // paint, not the frame it was armed for.
    for (const handle of presentationFrames) cancelFrame(handle);
    presentationFrames.clear();
  }
  function unavailable(reason: VisualCompositionStatus["blocker"]) {
    // Preserve the first specific failure until recovery. A later generic runtime notification
    // must not relabel invalid scenes or exceeded limits as missing media.
    if (value.status === "blocked") return;
    ++requestIntent;
    stopScheduling();
    if (ownsCanvas()) presentation = painter.clear(++generation);
    notify(frameListeners);
    publish("blocked", reason);
    if (reason === "resource_limit") {
      // IMPORTANT: an exceeded budget must release owners, not retain allocations while paused.
      // Keep the blocker and owner references until recovery can confirm authority cleanup.
      void Promise.allSettled([runtime?.close(), resources?.close()]);
    } else {
      void runtime?.pause().catch(() => undefined);
      releasePrepared();
    }
  }
  function onContextLost(event: Event) {
    event.preventDefault();
    contextLost = true;
    unavailable("canvas_unavailable");
  }
  function onContextRestored() {
    contextLost = false;
  }
  options.canvas.addEventListener("contextlost", onContextLost);
  options.canvas.addEventListener("contextrestored", onContextRestored);

  function createOwners() {
    const ownedBinding = binding;
    resources = createVisualCompositionResources({
      ...binding,
      ...(options.playbackScheduler === undefined
        ? {}
        : { playbackScheduler: options.playbackScheduler }),
      onFailure: () => {
        if (!closed) unavailable("source_unavailable");
      },
    });
    const ownedResources = resources;
    runtime = (options.runtimeFactory ?? createAuthoringLeasedEditorRuntime)({
      snapshot: ownedBinding.snapshot,
      manifest: ownedBinding.manifest,
      capability: ownedBinding.capability,
      leaseClient: ownedResources.leaseClient,
      transportOptions: { seekObservation: "exact_source_pts" },
      resolveScene: async (frame, snapshot, context) => {
        const currentOwner = () =>
          !closed && resources === ownedResources && !context.signal.aborted;
        if (!currentOwner()) throw new Error("cancelled");
        // IMPORTANT: a retained runtime resolves against current authority. Capturing its opening
        // binding here would authorize old scenes after the same owners have accepted a revision.
        const wire = await binding.resolveScene(frame, snapshot, context);
        if (!currentOwner()) throw new Error("cancelled");
        let decoded: ResolvedCompositionScene;
        try {
          decoded = decodeResolvedScene(wire);
          if (
            decoded.frame !== frame ||
            decoded.publicFingerprint !== snapshot.publicFingerprint
          )
            throw new Error("invalid_contract");
        } catch {
          if (currentOwner()) unavailable("invalid_contract");
          throw new Error("invalid_contract");
        }
        try {
          await ownedResources.prepare(decoded, context.epoch, context.signal);
        } catch (error) {
          if (currentOwner()) unavailable(failureDisposition(error));
          throw error;
        }
        // IMPORTANT: an old resolver/resource completion cannot install a scene or clear a
        // successor's picture, even after the shared session has reopened and closed is false.
        if (!currentOwner()) throw new Error("cancelled");
        scene = { value: decoded, epoch: context.epoch };
        return wire;
      },
    });
    const ownedRuntime = runtime;
    // IMPORTANT: a paused canvas still depends on a live native source. Clear it on failure
    // notifications; waiting for the next seek leaves a stale successful preview visible.
    unsubscribeRuntime = ownedRuntime.subscribe(() => {
      if (
        !closed &&
        runtime === ownedRuntime &&
        ownedRuntime.snapshot().blocker
      )
        unavailable(runtimeDisposition(ownedRuntime.snapshot().blocker?.code));
    });
  }

  function paint(observePresentation = true) {
    const state = runtime?.snapshot();
    if (
      !state ||
      !resources ||
      !scene ||
      contextLost ||
      closed ||
      !ownsCanvas() ||
      value.status === "blocked" ||
      state.blocker ||
      scene.epoch !== state.epoch ||
      scene.value.frame !== state.outputFrame ||
      state.publicFingerprint !== binding.snapshot.publicFingerprint
    )
      throw new Error("source_unavailable");
    const videoLayers = scene.value.layers.filter(
      (layer) =>
        binding.snapshot.assets.find((asset) => asset.assetId === layer.assetId)
          ?.kind === "video",
    );
    if (state.sources.length !== videoLayers.length)
      throw new Error("source_unavailable");
    for (const layer of videoLayers) {
      const observed = state.sources.find(
        (row) => row.ownerId === layer.clipId,
      );
      if (
        !observed ||
        observed.assetId !== layer.assetId ||
        observed.sourceFrame !== layer.sourceFrame ||
        observed.sourcePts !== layer.sourcePts ||
        (state.status === "paused"
          ? observed.observedSourcePts !== layer.sourcePts
          : observed.observedSourcePts === undefined ||
            observed.observedSourcePts < layer.sourcePts) ||
        !observed.observation
      )
        throw new Error("source_unavailable");
    }
    // IMPORTANT: paint only a paused exact observation. Native playback callbacks retain their
    // seek anchor while the media moves; treating that anchor as the current frame paints drift.
    const paintStarted = performance.now();
    const draft = visualTransformDraft;
    const presentedScene =
      draft === null
        ? scene.value
        : Object.freeze({
            ...scene.value,
            layers: Object.freeze(
              scene.value.layers.map((layer) =>
                layer.clipId === draft.clipId
                  ? Object.freeze({
                      ...layer,
                      transform: draft.transform,
                    })
                  : layer,
              ),
            ),
          });
    presentation = painter.render(
      presentedScene,
      resources.read(scene.value, state.epoch),
      ++generation,
    );
    const paintDuration = performance.now() - paintStarted;
    // IMPORTANT (M25-51): pointer previews are drafts, not delivered accepted-frame samples.
    // Publishing one through the measurement harness rerenders the overlay during pointer capture,
    // replaces its owner node, and loses the single release/command boundary.
    const observer = observePresentation
      ? options.onPresentationMeasurement
      : undefined;
    if (observer !== undefined && presentation.status === "presented") {
      const receipt = presentation;
      const rung = painter.path();
      const handle = requestFrame(() => {
        presentationFrames.delete(handle);
        try {
          observer({
            paintMs: paintDuration,
            deliveredMs: performance.now() - paintStarted,
            path: rung,
            backingWidth: receipt.previewWidth,
            backingHeight: receipt.previewHeight,
            renderedLayerCount: receipt.renderedLayerCount,
            frame: receipt.frame,
            generation: receipt.generation,
            publicFingerprint: receipt.publicFingerprint,
          });
        } catch {
          // Measurement observers cannot change a successful render or own source cleanup.
        }
      });
      presentationFrames.add(handle);
    }
    if (observePresentation) notify(frameListeners);
    if (
      presentation.status !== "presented" &&
      presentation.blocker === "canvas_unavailable"
    )
      throw new CanvasPresentationUnavailable();
    if (presentation.status !== "presented")
      throw new Error(presentation.blocker ?? "source_unavailable");
  }

  function currentRequest(token: number, ownRuntime: EditorRuntime) {
    return !closed && token === requestIntent && runtime === ownRuntime;
  }
  function trackPreparation(request: Promise<unknown>) {
    const task = request.then(
      () => undefined,
      () => undefined,
    );
    preparing = task;
    void task.then(() => {
      if (preparing === task) preparing = undefined;
    });
  }
  /**
   * Ask the runtime to open the incoming owner of the next ownership change while the outgoing
   * one still plays, so the move at the change finds it instead of acquiring it.
   *
   * IMPORTANT: the incoming clip's audio is scheduled ahead and starts on time; an acquisition
   * that only begins at the cut puts the picture behind the sound by its whole duration (half a
   * second was measured) for the rest of the clip. Best effort by design: without headroom, on a
   * refusal or on a failure the handoff is exactly what it was, and one change is asked for once
   * per runtime epoch. The scenes come from the binding's own resolver, never from the runtime's
   * -- that one prepares static resources and replaces this session's current scene, which a
   * frame that is not presented must not do.
   */
  function prepareUpcoming() {
    const ownRuntime = runtime;
    const state = ownRuntime?.snapshot();
    if (
      !playing ||
      closed ||
      preparing !== undefined ||
      ownRuntime?.prepare === undefined ||
      state?.status !== "playing" ||
      state.outputFrame === null
    )
      return;
    const from = state.outputFrame;
    if (
      lookahead?.epoch === state.epoch &&
      (lookahead.frame === from ||
        (lookahead.target !== null && from < lookahead.target))
    )
      return;
    const plan = planPreviewLookahead(binding.snapshot, from);
    lookahead = Object.freeze({
      frame: from,
      epoch: state.epoch,
      target: plan?.frames[0] ?? null,
    });
    if (plan === null) return;
    const ownBinding = binding;
    const prepare = ownRuntime.prepare;
    trackPreparation(
      (async () => {
        const context = Object.freeze({
          epoch: state.epoch,
          signal: new AbortController().signal,
        });
        const upcoming: unknown[] = [];
        for (const next of plan.frames)
          upcoming.push(
            await ownBinding.resolveScene(next, ownBinding.snapshot, context),
          );
        if (closed || runtime !== ownRuntime || !playing) return;
        await prepare(upcoming);
      })(),
    );
  }
  /** Give the prepared owner back: playback has stopped, so nothing is about to change. */
  function releasePrepared() {
    lookahead = undefined;
    const ownRuntime = runtime;
    if (ownRuntime?.prepare === undefined) return;
    const prepare = ownRuntime.prepare;
    trackPreparation(
      (preparing ?? Promise.resolve()).then(() =>
        closed || playing || runtime !== ownRuntime ? undefined : prepare([]),
      ),
    );
  }
  async function seekExact(
    frame: number,
    token: number,
    ownRuntime: EditorRuntime,
    pauseBefore: boolean,
  ): Promise<boolean> {
    if (!currentRequest(token, ownRuntime) || contextLost) return false;
    if (pauseBefore) {
      const pauseReceipt = await ownRuntime.pause();
      if (!currentRequest(token, ownRuntime)) return false;
      if (
        pauseReceipt.status !== "applied" &&
        ownRuntime.snapshot().status !== "paused"
      )
        throw new Error("source_unavailable");
    }
    const receipt = await ownRuntime.seek(frame);
    if (!currentRequest(token, ownRuntime) || receipt.status === "cancelled")
      return false;
    if (receipt.status !== "applied") throw new Error("source_unavailable");
    const paused = await ownRuntime.pause();
    if (!currentRequest(token, ownRuntime)) return false;
    if (paused.status !== "applied") throw new Error("source_unavailable");
    // IMPORTANT: frame equality is not request identity. A repeated-frame seek must fence an
    // older completion after the newer M12 epoch has physically aborted its resolver/transport.
    if (!currentRequest(token, ownRuntime)) return false;
    paint();
    if (playing && frame === value.durationFrames - 1) {
      // IMPORTANT: stop at a painted endpoint before resuming native media. Endpoint play can
      // reject or time out and erase the valid final frame before the outer scheduler observes it.
      stopScheduling();
      publish("paused");
      return true;
    }
    if (playing) {
      const resumed = await ownRuntime.play();
      if (!currentRequest(token, ownRuntime)) return false;
      if (resumed.status !== "applied") throw new Error("source_unavailable");
    }
    return true;
  }
  function requestMove(frame: number, pauseBefore: boolean) {
    const token = ++requestIntent;
    const ownRuntime = runtime;
    if (
      !Number.isSafeInteger(frame) ||
      frame < 0 ||
      frame >= value.durationFrames
    ) {
      unavailable("invalid_contract");
      return { token, task: Promise.resolve(false) };
    }
    if (!ownRuntime || closed || contextLost)
      return { token, task: Promise.resolve(false) };
    // IMPORTANT: invoke M12 seek for every superseding intent. Caller-only promise coalescing
    // leaves the obsolete resolver and transport alive instead of aborting their runtime epoch.
    const task = seekExact(frame, token, ownRuntime, pauseBefore).catch(
      (error: unknown) => {
        if (currentRequest(token, ownRuntime))
          unavailable(failureDisposition(error));
        return false;
      },
    );
    pendingMoves.add(task);
    void task.finally(() => pendingMoves.delete(task));
    return { token, task };
  }
  async function manualMove(frame: number) {
    const runtimeState = runtime?.snapshot();
    // IMPORTANT: explicit seek owns epoch supersession. A second pause while native play/pause is
    // pending is rejected and would falsely block the newer seek before it can abort that epoch.
    const pauseBefore =
      pendingMoves.size === 0 &&
      runtimeState?.status !== "seeking" &&
      runtimeState?.resources.pendingOperations === 0;
    manualMoves += 1;
    const request = requestMove(frame, pauseBefore);
    try {
      const applied = await request.task;
      if (applied && request.token === requestIntent) {
        if (playing) {
          startFrame = presentation?.frame ?? frame;
          startTime = now();
        } else if (value.status === "playing") publish("paused");
      }
    } finally {
      manualMoves -= 1;
    }
  }
  function schedule() {
    if (!playing || closed || scheduled !== undefined) return;
    scheduled = requestFrame(() => {
      scheduled = undefined;
      step();
    });
  }
  /**
   * Arm one step for the wall time of the next ownership change.
   *
   * IMPORTANT: a cut is otherwise found on the first display tick after it. The incoming
   * clip's audio starts on its own clock at the cut, so that tick's phase -- up to 16.7 ms at
   * 60 Hz, 15.5 ms measured -- is the incoming picture starting that much later, on top of the
   * move itself. The step is exactly the one a display tick runs; only its time is the cut's.
   * It is armed only within the last ticks before the change and is cleared with the schedule,
   * and a step that finds nothing to do (the outgoing owner's last frame not observed yet, a
   * clock rebased since) changes nothing: the next display tick still does what it always did.
   */
  function armBoundaryStep() {
    if (boundaryStep !== undefined || !playing || closed) return;
    const from = presentation?.frame;
    if (from === null || from === undefined) return;
    if (
      nextChange === undefined ||
      from < nextChange.from ||
      (nextChange.frame !== null && from >= nextChange.frame)
    )
      nextChange = Object.freeze({
        from,
        frame: nextVideoOwnerChange(binding.snapshot, from),
      });
    if (nextChange.frame === null) return;
    const delay =
      startTime + ((nextChange.frame - startFrame) * 1000) / 24 - now();
    if (delay <= 0 || delay > BOUNDARY_STEP_WINDOW_MS) return;
    boundaryStep = setTimer(() => {
      boundaryStep = undefined;
      step();
    }, Math.ceil(delay));
  }
  /** One step of playback: a display tick, or the step armed for an ownership change. */
  function step() {
    if (!playing || closed) return;
    if (presentation?.frame === value.durationFrames - 1) {
      void pause();
      return;
    }
    prepareUpcoming();
    const target = Math.min(
      value.durationFrames - 1,
      startFrame + Math.floor(((now() - startTime) * 24) / 1000),
    );
    if (
      target !== presentation?.frame &&
      scheduledMove === undefined &&
      manualMoves === 0
    ) {
      const ownRuntime = runtime;
      const token = requestIntent;
      const task =
        ownRuntime === undefined
          ? Promise.resolve(false)
          : ownRuntime.advance(target).then(async (receipt) => {
              if (!currentRequest(token, ownRuntime)) return false;
              if (receipt.status === "applied") {
                // IMPORTANT: an applied receipt can be a native-clock no-op after wall time ran
                // ahead of the decoder. Repainting the unchanged frame every RAF starves the
                // next RVFC and freezes continuous playback while the hidden video runs on.
                if (ownRuntime.snapshot().outputFrame === presentation?.frame)
                  return true;
                paint();
                return true;
              }
              if (receipt.blocker?.code === "unsupported") {
                // IMPORTANT: media controls are reserved for a real owner/source boundary.
                // Reintroducing requestMove for steady frames restores the audible seek loop.
                // CRITICAL: resolve the next unpresented frame, then rebase the clock after
                // acquisition. Seeking the distant wall target skips transition frames when
                // owner/resource handoff is slow; retaining that clock repeats the catch-up.
                const nextFrame = Math.min(
                  target,
                  (presentation?.frame ?? target) + 1,
                );
                // CRITICAL: when the receipt names the owner that ended and wall time has
                // reached its end, move to that end frame -- the cut. The runtime can report
                // the end before the owner's last frame was presented; "the next unpresented
                // frame" is then still inside that owner, and the audio follower treats a seek
                // there as a user seek and stops the audio it had scheduled across the cut
                // (a 21-32 ms silence, then the successor restarts late). Do not simplify this
                // back to nextFrame; an unnamed block, or a named owner whose end wall time
                // has not reached, keeps nextFrame so a clip is never skipped to its end.
                const ended = binding.snapshot.clips.find(
                  (clip) => clip.clipId === receipt.blocker?.subjectId,
                );
                const cutFrame =
                  ended === undefined
                    ? null
                    : ended.startFrame + ended.durationFrames;
                const boundaryFrame =
                  cutFrame !== null &&
                  cutFrame > nextFrame &&
                  cutFrame <= target
                    ? cutFrame
                    : nextFrame;
                const request = requestMove(boundaryFrame, true);
                const applied = await request.task;
                if (applied && request.token === requestIntent && playing) {
                  startFrame = presentation?.frame ?? boundaryFrame;
                  startTime = now();
                }
                return applied;
              }
              throw new Error(receipt.blocker?.code ?? "source_unavailable");
            });
      scheduledMove = task;
      void task
        .then(() => {
          // IMPORTANT: a newer seek can coalesce the endpoint into this move. Use the settled
          // presentation, or playback can remain playing forever at the last frame.
          if (
            ownRuntime !== undefined &&
            currentRequest(token, ownRuntime) &&
            presentation?.frame === value.durationFrames - 1
          )
            void pause();
        })
        .catch((error: unknown) => {
          // IMPORTANT: an obsolete advance may reject while its epoch is drained by a newer scrub.
          // Only its own still-current intent may invalidate the presented scene.
          if (ownRuntime !== undefined && currentRequest(token, ownRuntime))
            unavailable(failureDisposition(error));
        })
        .finally(() => {
          if (scheduledMove === task) scheduledMove = undefined;
        });
    }
    armBoundaryStep();
    schedule();
  }
  async function pause() {
    stopScheduling();
    const ownRuntime = runtime;
    if (!ownRuntime || closed) return;
    const state = ownRuntime.snapshot();
    if (
      (pendingMoves.size > 0 ||
        state.status === "seeking" ||
        state.resources.pendingOperations > 0) &&
      presentation?.frame !== null &&
      presentation?.frame !== undefined
    ) {
      // IMPORTANT: a playing runtime can still own a pending advance, even with no manual
      // move. Direct pause then refuses healthy media. Seek supersedes/drains that epoch at
      // the last presented frame before publishing pause; do not bypass the runtime guard.
      const request = requestMove(presentation.frame, false);
      await request.task;
      if (request.token === requestIntent && value.status !== "blocked")
        publish("paused");
      releasePrepared();
      return;
    }
    const token = ++requestIntent;
    const receipt = await ownRuntime.pause();
    // A paused preview holds only what it shows; the prepared owner's leases go back to the pool.
    releasePrepared();
    if (!currentRequest(token, ownRuntime)) return;
    if (!closed && value.status !== "blocked") {
      if (receipt?.status === "applied") publish("paused");
      else unavailable("source_unavailable");
    }
  }
  async function releaseOwners() {
    ++requestIntent;
    stopScheduling();
    closed = true;
    visualTransformDraft = null;
    unsubscribeRuntime?.();
    unsubscribeRuntime = undefined;
    publish("closing");
    if (ownsCanvas()) presentation = painter.clear(++generation);
    notify(frameListeners);
    lookahead = undefined;
    nextChange = undefined;
    if (preparing !== undefined) {
      // IMPORTANT: let a preparation in flight finish before the owners are closed. The lease
      // route admits one acquisition at a time and an abandoned one can keep its claim for a
      // while, so the binding that replaces this one would have its first acquisition refused.
      // Bounded: past the deadline the runtime's close aborts the preparation as a last resort.
      let timer: ReturnType<typeof setTimeout> | undefined;
      await Promise.race([
        (async () => {
          while (preparing !== undefined) await preparing;
        })(),
        new Promise<void>((resolve) => {
          timer = setTimeout(resolve, PREPARATION_SETTLE_DEADLINE_MS);
        }),
      ]);
      if (timer !== undefined) clearTimeout(timer);
    }
    // Native and static local owners must revoke together. Awaiting the native server release
    // first would keep image/font pixels alive through an unrelated authority timeout.
    const [native, staticResult] = await Promise.allSettled([
      runtime?.close(),
      resources?.close(),
    ]);
    let clean =
      native.status === "fulfilled" &&
      native.value?.blocker == null &&
      staticResult.status === "fulfilled";
    if (
      resources &&
      (resources.snapshot().staticLeases ||
        resources.snapshot().videoOwners ||
        resources.snapshot().pendingOperations)
    )
      clean = false;
    if (clean) {
      runtime = undefined;
      resources = undefined;
      scene = undefined;
    }
    publish("closed", clean ? null : "cleanup_pending");
    return clean;
  }
  function closeOwners(): Promise<boolean> {
    if (closing) return closing;
    // IMPORTANT: replace/recover/unmount may overlap. Share one release transaction so an
    // older completion cannot erase new owners or issue duplicate server releases.
    const task = releaseOwners().finally(() => {
      if (closing === task) closing = undefined;
    });
    closing = task;
    return task;
  }
  function resolveInitialFrame(source: InitialFrame): number {
    const requested = typeof source === "function" ? source() : source;
    if (!Number.isSafeInteger(requested)) throw new Error("invalid_contract");
    return Math.max(0, Math.min(value.durationFrames - 1, requested));
  }
  async function settleInitialFrame(
    source: InitialFrame,
    openedRuntime: EditorRuntime,
  ): Promise<boolean> {
    let target = resolveInitialFrame(source);
    while (openedRuntime.snapshot().outputFrame !== target) {
      const receipt = await openedRuntime.seek(target);
      if (closed || runtime !== openedRuntime || receipt.status === "cancelled")
        return false;
      if (receipt.status !== "applied") throw new Error("source_unavailable");
      const paused = await openedRuntime.pause();
      if (closed || runtime !== openedRuntime) return false;
      if (paused.status !== "applied") throw new Error("source_unavailable");
      const latest = resolveInitialFrame(source);
      if (latest === target) return true;
      target = latest;
    }
    return true;
  }
  async function open(initialFrame?: InitialFrame) {
    if (!closed) return;
    if (contextLost) {
      unavailable("canvas_unavailable");
      return;
    }
    if ((runtime || resources) && !(await closeOwners())) return;
    closed = false;
    publish("opening");
    try {
      createOwners();
      const openedRuntime = runtime!;
      const receipt = await openedRuntime.open(
        binding.manifest,
        binding.snapshot,
        RUNTIME_PROFILE_FINGERPRINT,
      );
      if (closed || runtime !== openedRuntime) return;
      if (receipt.status !== "applied") throw new Error("source_unavailable");
      if (
        initialFrame !== undefined &&
        !(await settleInitialFrame(initialFrame, openedRuntime))
      )
        return;
      // Adopt the pane's measurement before the first paint, so the first frame is presented at
      // the size the view actually has rather than at the compositor's floor. `painter.resize`
      // does nothing when the quantized size is unchanged, so this costs nothing when the box was
      // already delivered while open (B-M2545-42).
      if (measuredBox !== null) painter.resize(measuredBox);
      // GUARD: a restored binding must seek to the latest logical frame before its first paint.
      // Painting the runtime's bootstrap frame here causes a visible frame-zero commit during
      // replace/recovery and falsely acknowledges media that the workspace never requested.
      paint();
      publish("paused");
    } catch (error) {
      if (!closed) unavailable(failureDisposition(error));
    }
  }
  function canRebind(next: VisualCompositionBinding): boolean {
    const state = runtime?.snapshot();
    if (
      closed ||
      closing ||
      contextLost ||
      !ownsCanvas() ||
      !runtime ||
      !resources ||
      (state?.status !== "paused" && state?.status !== "playing") ||
      (value.status !== "paused" && value.status !== "playing") ||
      binding.leaseClient !== next.leaseClient ||
      binding.resolveScene !== next.resolveScene ||
      binding.snapshot.workspaceHandle !== next.snapshot.workspaceHandle ||
      binding.snapshot.profileId !== next.snapshot.profileId ||
      binding.snapshot.output.width !== next.snapshot.output.width ||
      binding.snapshot.output.height !== next.snapshot.output.height ||
      binding.manifest.profileFingerprint !==
        next.manifest.profileFingerprint ||
      !isEvaluatedAvailableDisposition(next.capability)
    )
      return false;
    try {
      validatePublicAssetManifest(next.manifest, next.snapshot);
    } catch {
      return false;
    }
    const before = binding.capability;
    const after = next.capability;
    return (
      before.status === after.status &&
      before.blocker === after.blocker &&
      before.profileFingerprint === after.profileFingerprint &&
      before.engineProfileId === after.engineProfileId &&
      before.fallback === after.fallback &&
      before.frameObserver === after.frameObserver &&
      before.qualified === after.qualified &&
      Object.keys(before.limits).length === Object.keys(after.limits).length &&
      (Object.keys(before.limits) as Array<keyof typeof before.limits>).every(
        (key) => before.limits[key] === after.limits[key],
      )
    );
  }
  async function settleRebind() {
    // IMPORTANT: direct controls wait for current owner authority. Seeking/playing during
    // reauthorization cancels its runtime epoch and can block or overwrite the user's move.
    while (rebinding !== undefined) await rebinding;
  }
  async function rebind(
    next: VisualCompositionBinding,
    intent: number,
    initialFrame?: InitialFrame,
  ): Promise<boolean> {
    const ownRuntime = runtime!;
    const ownResources = resources!;
    const heldFrame =
      presentation?.frame ?? ownRuntime.snapshot().outputFrame ?? 0;
    const current = () =>
      intent === lifecycleIntent &&
      !closed &&
      runtime === ownRuntime &&
      resources === ownResources &&
      ownsCanvas();
    try {
      await pause();
      if (!current()) return true;
      if (value.status === "blocked") {
        binding = next;
        return true;
      }
      let timer: ReturnType<typeof setTimeout> | undefined;
      const settled = await Promise.race([
        (async () => {
          while (preparing !== undefined) await preparing;
          return true;
        })(),
        new Promise<boolean>((resolve) => {
          timer = setTimeout(
            () => resolve(false),
            PREPARATION_SETTLE_DEADLINE_MS,
          );
        }),
      ]).finally(() => {
        if (timer !== undefined) clearTimeout(timer);
      });
      if (!current()) return true;
      if (!settled || !painter.replace(next.snapshot)) return false;
      binding = next;
      lookahead = undefined;
      nextChange = undefined;
      publish("paused");
      await ownResources.replace(next.manifest, next.snapshot);
      if (!current()) return true;
      const target = () => resolveInitialFrame(initialFrame ?? heldFrame);
      const receipt = await ownRuntime.replace(
        next.manifest,
        next.snapshot,
        target,
      );
      if (!current()) return true;
      if (receipt.status !== "applied") throw new Error("source_unavailable");
      if (!(await settleInitialFrame(target, ownRuntime)) || !current())
        return true;
      if (measuredBox !== null) painter.resize(measuredBox);
      // IMPORTANT: the retained picture stays untouched through authority/seek work. Paint only
      // the newest settled target, never a bootstrap frame or a stale replacement completion.
      paint();
      publish("paused");
    } catch (error) {
      if (current()) {
        binding = next;
        unavailable(failureDisposition(error));
      }
    }
    return true;
  }
  return Object.freeze({
    open,
    canRebind,
    /**
     * Adopt a newly measured picture box.
     *
     * IMPORTANT (M25-45): the session owns this, not the observer. A `ResizeObserver` fires per
     * frame during a splitter drag, so the requests are coalesced to one per animation frame and
     * the last one wins. The repaint goes through `paint`, which already fences on ownership, the
     * runtime epoch, the presented frame, the public fingerprint and an exact paused observation
     * -- so a resize queued behind a seek, a replacement, a revoked lease or a close cannot paint
     * old pixels or publish success for a surface the session no longer owns. `painter.resize`
     * does no work at all when the quantized size is unchanged, which is the common case while
     * dragging, and it never repaints the previous frame itself.
     */
    resize(box: PreviewBox) {
      // The measurement is recorded before the liveness checks, because it is true regardless of
      // whether this surface can act on it yet; `open` adopts it. See `measuredBox` (B-M2545-42).
      measuredBox = box;
      if (closed || rebinding || !ownsCanvas()) return;
      pendingBox = box;
      if (scheduledResize !== undefined) return;
      scheduledResize = requestFrame(() => {
        scheduledResize = undefined;
        const next = pendingBox;
        pendingBox = null;
        if (next === null || closed || !ownsCanvas() || contextLost) return;
        const receipt = painter.resize(next);
        if (
          receipt.previewWidth === presentation?.previewWidth &&
          receipt.previewHeight === presentation?.previewHeight
        )
          return;
        if (value.status !== "paused") return;
        // GUARD (B-M2545-28): a repaint is owed by the backing, never intended by a user, so a
        // transport move in flight owns the next paint and this one stands down. `paint` refuses by
        // throwing -- the resolved scene still belongs to the epoch the move superseded -- and the
        // published status is still `paused` throughout a move, so without this guard the throw
        // reaches the catch below and a resize turns a healthy seek into `blocked`/
        // `source_unavailable`. Nothing then recovers: `paint` refuses on its first guard while
        // blocked, so the source sits alive, decoded and correct behind a monitor reading "source
        // unavailable" until a user presses Recover. Measured directly: two 1280 x 720 corpus rows
        // delivered zero paints in 120 s each, the publisher named itself as this call site, and the
        // window is the restore seek that every row after the first issues into a monitor whose
        // picture box has just been remeasured. Do not "fix" this by swallowing the paint failure
        // instead -- a genuine canvas or source failure outside a move must still latch -- and do
        // not drop the adopted size: `painter.resize` has already taken it, and the settling move's
        // own `paint` presents at it, which `visualCompositionSession.test.ts` asserts.
        if (pendingMoves.size > 0 || runtime?.snapshot().status === "seeking")
          return;
        try {
          paint();
        } catch (error) {
          unavailable(failureDisposition(error));
        }
      });
    },
    /**
     * Repaint one paused resolved layer with an in-memory transform draft.
     *
     * IMPORTANT (M25-51): this is a presentation draft, never a snapshot mutation. Keeping it
     * inside the session preserves the exact resolved frame/resources while the DOM overlay moves;
     * routing pointer motion through timeline commands would create an undo step per event.
     */
    previewVisualTransform(
      clipId: string,
      transform: Readonly<Record<string, number>> | null,
    ): VisualLayerGeometry | null {
      if (
        closed ||
        rebinding ||
        value.status !== "paused" ||
        scene === undefined ||
        runtime?.snapshot().epoch !== scene.epoch ||
        !scene.value.layers.some((layer) => layer.clipId === clipId)
      )
        return null;
      if (transform === null) {
        if (visualTransformDraft?.clipId !== clipId)
          return painter.layerGeometry(clipId);
        visualTransformDraft = null;
      } else {
        const bounds = {
          anchor_x_bp: [0, 10_000],
          anchor_y_bp: [0, 10_000],
          position_x_bp: [-40_000, 40_000],
          position_y_bp: [-40_000, 40_000],
          scale_x_bp: [1, 80_000],
          scale_y_bp: [1, 80_000],
          rotation_mdeg: [-180_000, 180_000],
        } as const;
        if (
          Object.keys(transform).length !== Object.keys(bounds).length ||
          Object.entries(bounds).some(([key, [minimum, maximum]]) => {
            const member = transform[key];
            return (
              !Number.isSafeInteger(member) ||
              member < minimum ||
              member > maximum
            );
          })
        )
          return null;
        visualTransformDraft = Object.freeze({
          clipId,
          transform: Object.freeze({ ...transform }),
        });
      }
      try {
        paint(false);
      } catch {
        return null;
      }
      return painter.layerGeometry(clipId);
    },
    getVisualLayerGeometry(clipId: string): VisualLayerGeometry | null {
      return painter.layerGeometry(clipId);
    },
    async seek(frame: number) {
      if (rebinding) await settleRebind();
      if (closed) return;
      await manualMove(frame);
    },
    async step(direction: -1 | 1) {
      if (direction !== -1 && direction !== 1) return;
      if (rebinding) await settleRebind();
      const intent = requestIntent;
      await pause();
      // IMPORTANT: the step belongs to its pause intent. An older awaited pause must not move
      // again after a newer explicit seek has already superseded it.
      if (closed || requestIntent !== intent + 1 || value.status !== "paused")
        return;
      await manualMove(
        Math.max(
          0,
          Math.min(
            value.durationFrames - 1,
            (presentation?.frame ?? 0) + direction,
          ),
        ),
      );
    },
    async play() {
      if (rebinding) await settleRebind();
      if (closed || value.status !== "paused") return;
      // IMPORTANT: a paused endpoint is terminal. Starting native media here can fail before the
      // RAF issues its pause and clear an already valid final-frame presentation.
      if (presentation?.frame === value.durationFrames - 1) {
        stopScheduling();
        publish("paused");
        return;
      }
      const ownRuntime = runtime;
      if (!ownRuntime) return;
      const token = requestIntent;
      const receipt = await ownRuntime.play();
      // IMPORTANT: a newer seek aborts pending native play. Fence its cancelled completion or
      // this older Play intent clears and blocks the newer exact-frame presentation.
      if (!currentRequest(token, ownRuntime)) return;
      if (receipt?.status !== "applied") {
        unavailable("source_unavailable");
        return;
      }
      playing = true;
      startFrame = presentation?.frame ?? 0;
      startTime = now();
      publish("playing");
      schedule();
    },
    async pause() {
      if (rebinding) await settleRebind();
      await pause();
    },
    async recover(initialFrame?: InitialFrame) {
      const intent = ++lifecycleIntent;
      if ((await closeOwners()) && intent === lifecycleIntent) {
        if (!ownsCanvas()) return;
        painter.close();
        painter = createVisualCompositor(options.canvas, binding.snapshot);
        await open(initialFrame);
      }
    },
    async replace(next: VisualCompositionBinding, initialFrame?: InitialFrame) {
      const intent = ++lifecycleIntent;
      visualTransformDraft = null;
      if (rebinding) await rebinding;
      if (intent !== lifecycleIntent) return;
      const requested = projectBinding(next);
      if (canRebind(requested)) {
        const task = rebind(requested, intent, initialFrame);
        rebinding = task;
        let retained: boolean;
        try {
          retained = await task;
        } finally {
          if (rebinding === task) rebinding = undefined;
        }
        if (retained || intent !== lifecycleIntent) return;
      }
      // IMPORTANT: retain the newest requested authority even when old-owner release fails.
      // Recovery must not reopen the obsolete binding after React has already accepted next.
      binding = requested;
      if (!(await closeOwners()) || intent !== lifecycleIntent) return;
      if (!ownsCanvas()) return;
      painter.close();
      painter = createVisualCompositor(options.canvas, binding.snapshot);
      await open(initialFrame);
    },
    async close() {
      ++lifecycleIntent;
      // Keep this owner reachable through one bounded retry even after React unmount drops its
      // reference. A persistent refusal stays cleanup_pending rather than claiming false zero.
      if (!(await closeOwners())) await closeOwners();
      if (value.blocker === null && ownsCanvas()) {
        painter.close();
        canvasOwners.delete(options.canvas);
      }
      options.canvas.removeEventListener("contextlost", onContextLost);
      options.canvas.removeEventListener("contextrestored", onContextRestored);
    },
    getSnapshot: () => value,
    getPresentation: () => presentation,
    getResources: () =>
      resources?.snapshot() ??
      Object.freeze({
        imageOwners: 0,
        fontOwners: 0,
        videoOwners: 0,
        staticLeases: 0,
        pendingOperations: 0,
        blobBytes: 0,
      }),
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    subscribeFrame(listener: () => void) {
      frameListeners.add(listener);
      return () => {
        frameListeners.delete(listener);
      };
    },
  });
}
