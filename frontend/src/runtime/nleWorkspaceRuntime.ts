// M25-16 monitor runtime adapter.
//
// The expanded workspace owns one composition monitor. It is assembled here from the accepted
// runtime pieces only: the M25-12 leased runtime and its capability disposition, the M25-13
// composition session, the M25-15 embedded-audio follower and the ported scene resolver. The
// adapter adds no decoding, playback or rendering of its own; it binds those pieces to one
// canvas, exposes their statuses and disposes them together.

import type { PublicCompositionSnapshot } from "../contracts/compositionCodec";
import type { EmbeddedAudioFollowerStatus } from "../contracts/embeddedAudioFollowerCodec";
import type { AuthoringMediaSourceLeaseClient } from "../host/authoringMediaSourceLease";
import type { NlePlaybackLeaseScheduler } from "../host/nleLeaseScheduler";
import type { AuthoringLeasedEditorRuntimeOptions } from "./authoringLeasedEditorRuntime";
import type { EditorRuntime } from "./editorRuntime";
import {
  createEmbeddedAudioFollower,
  type EmbeddedAudioFollower,
} from "./embeddedAudioFollower";
import type { RuntimeCapabilityDisposition } from "./mediaCapabilities";
import { buildPublicAssetManifest } from "./publicAssetManifest";
import { resolveCompositionScene } from "./sceneResolver";
import {
  createVisualCompositionSession,
  type PresentationMeasurement,
  type VisualCompositionBinding,
  type VisualCompositionSession,
  type VisualCompositionStatus,
} from "./visualCompositionSession";

export type NleMonitorMode =
  "composition" | "selected_source_only" | "unavailable";

export type NleMonitorBinding = Readonly<{
  mode: NleMonitorMode;
  binding: VisualCompositionBinding | null;
}>;

/**
 * Derive the monitor binding for a snapshot. `composition` needs the accepted-available
 * disposition; a disposition whose fallback is `selected_source_only` degrades to that mode
 * with no compositor; anything else leaves the monitor unavailable. The scene resolver is the
 * pure port and is bound once so the composition session sees a stable identity.
 */
export function deriveNleMonitorBinding(
  snapshot: PublicCompositionSnapshot | undefined,
  capability: RuntimeCapabilityDisposition,
  leaseClient: AuthoringMediaSourceLeaseClient,
): NleMonitorBinding {
  if (snapshot === undefined)
    return Object.freeze({ mode: "unavailable", binding: null });
  if (capability.status !== "available")
    return Object.freeze({
      mode:
        capability.fallback === "selected_source_only"
          ? "selected_source_only"
          : "unavailable",
      binding: null,
    });
  let manifest;
  try {
    manifest = buildPublicAssetManifest(snapshot);
  } catch {
    return Object.freeze({ mode: "unavailable", binding: null });
  }
  return Object.freeze({
    mode: "composition",
    binding: Object.freeze({
      snapshot,
      manifest,
      capability,
      resolveScene: boundSceneResolver,
      leaseClient,
    }),
  });
}

function boundSceneResolver(
  frame: number,
  snapshot: PublicCompositionSnapshot,
) {
  return resolveCompositionScene(snapshot, frame);
}

export type NleMonitorStatus = Readonly<{
  composition: VisualCompositionStatus;
  audio: EmbeddedAudioFollowerStatus | null;
  /** The transport's position: during a rebind, the kept frame (B-M2561-04). */
  frame: number | null;
  /**
   * B-M2564-12: the frame the canvas shows, or `null` while it shows none. It differs from
   * `frame` while a rebind's new owner shows frame 0 before it is sought back to the kept frame.
   */
  presentedFrame: number | null;
  /**
   * B-M2561-04: the monitor is moving to an accepted revision's binding. The transport stays
   * usable: seeks and Play are held for the new owner, and `frame` is the kept position.
   */
  rebinding?: boolean;
}>;

export type NleMonitor = Readonly<{
  session: VisualCompositionSession;
  status(): NleMonitorStatus;
  subscribe(listener: () => void): () => void;
  replace(
    binding: VisualCompositionBinding,
    logicalFrame?: number | (() => number),
  ): Promise<void>;
  dispose(): Promise<void>;
}>;

const CLOSED_COMPOSITION: VisualCompositionStatus = Object.freeze({
  status: "closed",
  blocker: null,
  browserPreviewOnly: true,
  durationFrames: 0,
});

/**
 * Create the monitor on a canvas. The embedded-audio follower is created inside the session's
 * runtime factory so the session keeps sole ownership of the transport lifecycle; the follower
 * is only observed here.
 */
export function createNleMonitor(
  canvas: HTMLCanvasElement,
  binding: VisualCompositionBinding,
  hooks: Readonly<{
    playbackScheduler?: NlePlaybackLeaseScheduler;
    onPresentationMeasurement?: (sample: PresentationMeasurement) => void;
  }> = {},
): NleMonitor {
  let follower: EmbeddedAudioFollower | undefined;
  let unsubscribeFollower: (() => void) | undefined;
  const listeners = new Set<() => void>();
  const notify = () => {
    for (const listener of listeners) {
      try {
        listener();
      } catch {
        /* observers never own transport */
      }
    }
  };
  const runtimeFactory = (
    options: AuthoringLeasedEditorRuntimeOptions,
  ): EditorRuntime => {
    unsubscribeFollower?.();
    // IMPORTANT: audio-only prefetch must use the same arbiter as visual acquisition, or
    // background preparation can hold the media worker while the next clip asks for sound.
    follower = createEmbeddedAudioFollower({
      ...options,
      ...(hooks.playbackScheduler === undefined
        ? {}
        : { playbackScheduler: hooks.playbackScheduler }),
    });
    unsubscribeFollower = follower.subscribe(() => publish());
    return follower.runtime;
  };
  const session = createVisualCompositionSession({
    ...binding,
    canvas,
    runtimeFactory,
    ...(hooks.playbackScheduler === undefined
      ? {}
      : { playbackScheduler: hooks.playbackScheduler }),
    ...(hooks.onPresentationMeasurement === undefined
      ? {}
      : { onPresentationMeasurement: hooks.onPresentationMeasurement }),
  });
  // IMPORTANT (B-M2561-04): the position the transport keeps across a rebind, until the new
  // owner has been sought there (or the user moves it). A rebind opens the new owner at frame 0;
  // publishing that presentation sent the playhead back to 0 on every accepted edit.
  let restoring: number | null = null;
  const compute = (): NleMonitorStatus => {
    const presentation = session.getPresentation();
    const presentedFrame =
      presentation?.status === "presented" && presentation.frame !== null
        ? presentation.frame
        : null;
    return Object.freeze({
      composition: session.getSnapshot() ?? CLOSED_COMPOSITION,
      audio: follower?.status() ?? null,
      frame: restoring ?? presentedFrame,
      presentedFrame,
    });
  };
  let value = compute();
  let replacement = 0;
  let replacing = false;
  let disposed = false;
  let opening: Promise<void> | null = null;
  let seeking: Promise<void> | null = null;
  // A pause requested while media is still opening or being replaced is retained as the
  // current owner's transport intent and applied once that owner is ready. It is never sent
  // to the owner being torn down, and a newer replacement re-queues it through its own settle.
  let pendingPause = false;
  let pendingSeek: number | null = null;
  // A Play pressed while a rebind is in flight, applied once the new owner is ready.
  let pendingPlay = false;
  let transportIntent = 0;
  // IMPORTANT (B-M2522-SEEK-01): a seek can be acquiring native media (a video proxy lease
  // create/open) when the next intent arrives. Superseding it aborts that browser request, but
  // the lease route keeps its no-queue claim until the abandoned native work has stopped, so the
  // newer acquisition is refused `busy` (429) and the monitor latches "source unavailable". Keep
  // one seek in flight, as for opening: later seeks retain only the newest frame, and pause and
  // replacement wait until it settles. A seek retained behind that seek settles only when its
  // frame has been sought or dropped: the slider steps from the requested frame until the seek
  // call settles (M25-21 B3-D10), so settling early loses every quick key press after the first.
  let seekWaiters: Array<() => void> = [];
  const releaseSeekWaiters = (waiters = seekWaiters) => {
    if (waiters === seekWaiters) seekWaiters = [];
    for (const resolve of waiters) resolve();
  };
  const trackSeek = (
    frame: number,
    settledIntent?: (applied: boolean) => void,
  ): Promise<void> => {
    const task = session.seek(frame);
    const inFlight = task.then(
      () => undefined,
      () => undefined,
    );
    seeking = inFlight;
    const clear = () => {
      if (seeking === inFlight) seeking = null;
    };
    return task.then(
      () => {
        clear();
        settledIntent?.(true);
      },
      (error: unknown) => {
        clear();
        settledIntent?.(false);
        throw error;
      },
    );
  };
  const settleTransportIntent = () => {
    if (disposed || replacing || opening !== null || seeking !== null) return;
    const frame = pendingSeek;
    pendingSeek = null;
    const intent = transportIntent;
    const owner = replacement;
    const current = () =>
      !disposed &&
      !replacing &&
      opening === null &&
      owner === replacement &&
      intent === transportIntent;
    const pauseCurrent = () => {
      if (!pendingPause || !current()) return;
      pendingPause = false;
      void session.pause().catch(() => undefined);
    };
    const playCurrent = () => {
      if (!pendingPlay || !current()) return;
      pendingPlay = false;
      void session.play().catch(() => undefined);
    };
    const endRestore = () => {
      if (restoring === null) return;
      restoring = null;
      publish();
    };
    if (frame === null) {
      endRestore();
      releaseSeekWaiters();
      pauseCurrent();
      playCurrent();
    } else {
      const waiters = seekWaiters;
      seekWaiters = [];
      void trackSeek(frame, (applied) => {
        endRestore();
        releaseSeekWaiters(waiters);
        if (applied) {
          pauseCurrent();
          playCurrent();
        }
        settleTransportIntent();
      }).catch(() => undefined);
    }
  };
  const trackOpening = (operation: () => Promise<void>): Promise<void> => {
    const task = operation();
    const settled = task.then(
      () => undefined,
      () => undefined,
    );
    opening = settled;
    void settled.then(() => {
      if (opening === settled) {
        opening = null;
        settleTransportIntent();
      }
    });
    return task;
  };
  const publish = () => {
    if (replacing) return;
    const next = compute();
    // A single transition reaches all three subscriptions. Preserve snapshot identity
    // when their projections agree or React commits the same monitor state repeatedly.
    // IMPORTANT (B-M2564-12): the presented frame is compared apart from `frame`. When a rebind's
    // new owner is sought back to the kept frame, that presentation changes nothing else here,
    // and it is what tells the monitor its picture -- and the layer geometry the transform
    // overlay reads when it renders -- is the kept frame's. Comparing `frame` alone published
    // nothing then: the overlay stayed absent until the playhead moved.
    if (
      next.composition === value.composition &&
      next.audio === value.audio &&
      next.frame === value.frame &&
      next.presentedFrame === value.presentedFrame &&
      next.rebinding === value.rebinding
    )
      return;
    value = next;
    notify();
  };
  const unsubscribeSession = session.subscribe(publish);
  const unsubscribeFrame = session.subscribeFrame(publish);
  const seek = async (frame: number) => {
    if (disposed) return;
    transportIntent++;
    // IMPORTANT: seeking an opening owner cancels its native acquisition. Keep only
    // the latest frame until the newest binding settles, as with deferred Pause.
    if (replacing || opening !== null) {
      pendingSeek = frame;
      // B-M2561-04: during a rebind the held seek is the position the transport keeps. The
      // call settles at once (the monitor clears the user's request), so the published
      // frame must already be the held one or the ruler jumps back to the kept frame.
      // IMPORTANT (B-M2564-04): also when the rebind began before any frame was presented
      // (a selection accepted during the first opening), where there is no kept frame yet.
      // Requiring one left the frame null while the press settled: the timeline cleared its
      // requested frame and rendered its tools from no playhead, and Split stayed disabled.
      if (replacing && restoring !== frame) {
        restoring = frame;
        value = Object.freeze({ ...value, frame });
        notify();
      }
      return;
    }
    if (seeking !== null) {
      pendingSeek = frame;
      return new Promise<void>((resolve) => seekWaiters.push(resolve));
    }
    pendingSeek = null;
    try {
      await trackSeek(frame);
    } finally {
      settleTransportIntent();
    }
  };
  return Object.freeze({
    session: Object.freeze({
      ...session,
      open: async (initialFrame?: number | (() => number)) => {
        if (disposed) return;
        if (opening !== null) return opening;
        // IMPORTANT: preserve the late-bound opening frame through this wrapper. Dropping this
        // argument reopens remounted media at frame 0 before first paint and defeats retention.
        await trackOpening(() => session.open(initialFrame));
      },
      pause: async () => {
        if (disposed) return;
        transportIntent++;
        pendingPlay = false;
        // IMPORTANT: trim begins while canonical edits may still be opening media.
        // An opening owner is not playing; pausing it now would cancel its native
        // acquisition and leave the next binding facing the backend's still-held busy
        // claim. Retain the intent instead; it applies when the owner is ready, so a
        // pause issued during opening still wins over an earlier pending play.
        if (replacing || opening !== null || seeking !== null) {
          pendingPause = true;
          return;
        }
        pendingPause = false;
        await session.pause();
      },
      seek,
      // IMPORTANT (B-M2564-09): a frame step during a rebind is a held seek from the position
      // the transport shows (the held seek, else the kept frame), like a ruler press. The
      // session's own step pauses first, and pausing an owner that is still opening is refused
      // and latches source_unavailable: a `.` straight after a selection broke the monitor.
      // During the first opening there is no position to step from, as for Play.
      step: async (direction: -1 | 1) => {
        if (disposed) return;
        if (replacing) {
          const from = value.frame;
          if (from === null) return;
          const last = Math.max(0, value.composition.durationFrames - 1);
          const target = Math.max(0, Math.min(last, from + direction));
          if (target !== from) await seek(target);
          return;
        }
        if (opening !== null) return;
        await session.step(direction);
      },
      play: async () => {
        if (disposed) return;
        // B-M2561-04: a Play during a rebind is held for the new owner, like a seek. During the
        // first opening the transport is unavailable and there is nothing to hold it for.
        if (replacing) {
          transportIntent++;
          pendingPause = false;
          pendingPlay = true;
          return;
        }
        if (opening !== null) return;
        transportIntent++;
        pendingPause = false;
        pendingPlay = false;
        await session.play();
      },
    }),
    status: () => value,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    async replace(next, logicalFrame) {
      if (disposed) return;
      const current = ++replacement;
      const retained = session.canRebind?.(next) === true;
      replacing = true;
      // IMPORTANT: an accepted composition edit reauthorizes the monitor against current history.
      // The position (a held seek, else the kept or presented frame) is clamped to the new duration;
      // compatible owners keep their presented picture while reauthorizing. Seeks
      // and Play are held meanwhile, and `rebinding` keeps the controls available. This used to
      // publish `frame: null` with the controls unavailable: the playhead returned to 0 and a
      // ruler press in the ~50 ms window was dropped.
      const durationFrames = Number(next.snapshot.output.durationFrames);
      const requestedLogicalFrame = () => {
        const requested =
          pendingSeek ??
          (typeof logicalFrame === "function"
            ? logicalFrame()
            : logicalFrame) ??
          restoring ??
          value.frame ??
          0;
        return Math.max(0, Math.min(requested, durationFrames - 1));
      };
      const kept = requestedLogicalFrame();
      restoring = kept;
      // Replacement is one visible operation, not a sequence of old-owner close,
      // follower detach and new-owner open states. Stale completions cannot publish old
      // state.
      value = Object.freeze({
        composition: Object.freeze<VisualCompositionStatus>({
          status: retained ? value.composition.status : "opening",
          blocker: null,
          browserPreviewOnly: true,
          durationFrames,
        }),
        audio:
          retained || value.audio === null
            ? value.audio
            : Object.freeze({
                ...value.audio,
                state: "suspended",
                reason: "opening",
              }),
        frame: restoring,
        presentedFrame: retained ? value.presentedFrame : null,
        rebinding: true,
      });
      notify();
      try {
        // IMPORTANT: finish native media opening before replacing its owner. Aborting
        // a pending open leaves the backend claim busy; overlapping replacements then
        // fail with 429. Only the newest queued binding may open after it settles.
        while (opening !== null || seeking !== null) await (opening ?? seeking);
        if (disposed || current !== replacement) return;
        // GUARD: the session must consume the latest logical position before its first paint.
        // Reintroducing a post-open restore seek commits bootstrap frame zero and duplicates the
        // media request that this replacement already performed.
        await trackOpening(() => session.replace(next, requestedLogicalFrame));
      } finally {
        if (current === replacement) {
          replacing = false;
          const presented = session.getPresentation()?.frame ?? null;
          if (presented !== null && pendingSeek === presented)
            pendingSeek = null;
          restoring = null;
          publish();
          settleTransportIntent();
        }
      }
    },
    async dispose() {
      disposed = true;
      releaseSeekWaiters();
      replacement++;
      replacing = true;
      unsubscribeSession();
      unsubscribeFrame();
      unsubscribeFollower?.();
      unsubscribeFollower = undefined;
      listeners.clear();
      await session.close();
      follower = undefined;
    },
  });
}

/** Whether two bindings would open the same monitor (mirrors the accepted preview rule). */
export function sameMonitorBinding(
  previous: VisualCompositionBinding | null,
  next: VisualCompositionBinding | null,
): boolean {
  if (previous === null || next === null) return previous === next;
  return (
    previous.snapshot.workspaceHandle === next.snapshot.workspaceHandle &&
    previous.snapshot.workspaceRevision === next.snapshot.workspaceRevision &&
    previous.snapshot.timelineRevision === next.snapshot.timelineRevision &&
    previous.snapshot.publicFingerprint === next.snapshot.publicFingerprint &&
    previous.manifest.manifestFingerprint ===
      next.manifest.manifestFingerprint &&
    previous.manifest.profileFingerprint === next.manifest.profileFingerprint &&
    // Capability changes must replace the session even when its status stays
    // available; otherwise the decoder keeps superseded limits or profile rules.
    previous.capability.status === next.capability.status &&
    previous.capability.blocker === next.capability.blocker &&
    previous.capability.profileFingerprint ===
      next.capability.profileFingerprint &&
    previous.capability.engineProfileId === next.capability.engineProfileId &&
    previous.capability.fallback === next.capability.fallback &&
    previous.capability.frameObserver === next.capability.frameObserver &&
    previous.capability.qualified === next.capability.qualified &&
    JSON.stringify(previous.capability.limits) ===
      JSON.stringify(next.capability.limits) &&
    previous.resolveScene === next.resolveScene &&
    previous.leaseClient === next.leaseClient
  );
}
