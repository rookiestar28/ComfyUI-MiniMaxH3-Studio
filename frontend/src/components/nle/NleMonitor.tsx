// M25-16 monitor: the accepted composition session bound to one canvas, with the
// extension-owned transport (play, pause, step, frame-domain slider) and the read-only
// embedded-audio status. Transport is `local_transport`: it issues no M25-11 command and
// touches no revision or history. No media element exposes UA controls.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { AuthoringPreviewMonitor } from "../AuthoringPreviewMonitor";
import type { AuthoringProjection } from "../../contracts/authoringWorkbenchCodec";
import type { AuthoringPreviewOpener } from "../../host/authoringFrameCoordinator";

import type { CompositionClip } from "../../contracts/compositionCodec";
import type { Locale } from "../../i18n/catalog";
import type { MediaToolsBinding } from "../../state/mediaRuntimeState";
import {
  createNleMonitor,
  sameMonitorBinding,
  type NleMonitor as MonitorHandle,
  type NleMonitorBinding,
  type NleMonitorStatus,
} from "../../runtime/nleWorkspaceRuntime";
import type {
  PresentationMeasurement,
  VisualCompositionBinding,
} from "../../runtime/visualCompositionSession";
import { monitorHeaderFacts } from "../../runtime/nleProjectSettings";
import { NleActionIcon, type NleIconName } from "./NleIconActions";
import { nleCopy, type NleCopy } from "./nleCopy";
import type { NleMonitorStatusChannel } from "./nleMonitorChannel";
import type { NlePlaybackLeaseScheduler } from "../../host/nleLeaseScheduler";
import { monitorUnavailableReason } from "./NleMonitorChips";
import { NleTransport } from "./NleTransport";
import { NleTransformOverlay } from "./NleTransformOverlay";
import { transformWire, type TransformWire } from "./nleCommandBuilders";
import {
  computePictureBox,
  type PictureBox,
  type PictureView,
} from "./nleMonitorGeometry";

const EMPTY_PICTURE: PictureBox = Object.freeze({
  width: 0,
  height: 0,
  left: 0,
  top: 0,
});

const CLOSED: NleMonitorStatus = Object.freeze({
  composition: Object.freeze({
    status: "closed",
    blocker: null,
    browserPreviewOnly: true,
    durationFrames: 0,
  }),
  audio: null,
  frame: null,
  presentedFrame: null,
});

function ignore(operation: Promise<unknown>): void {
  void operation.catch(() => undefined);
}

/** M25-64 (row #26): "Preview" and the accepted output's size and rate, when it states them. */
function MonitorHeader({
  text,
  facts,
}: {
  text: NleCopy;
  facts: string | null;
}) {
  return (
    <div className="h3-nle-monitor-header" data-h3-nle-monitor-header="">
      <span data-h3-nle-monitor-title="">{text.monitor.header}</span>
      {facts !== null ? (
        <span data-h3-nle-monitor-facts="">{facts}</span>
      ) : null}
    </div>
  );
}

/**
 * M25-64 (row #27): why the picture area has no picture, centred in it, with at most one action.
 *
 * IMPORTANT (A64-1, after B-M2563-09): the overlay never takes a pointer event except on its own
 * action. It paints over the picture, and a transform handle or the move target of a selected
 * layer can lie beneath it; the transport row is outside the picture area, and the overlay never
 * reaches it. `nleMonitorPicture.spec.ts` checks both with a box and an `elementFromPoint` probe.
 */
function PictureOverlay({
  kind,
  icon,
  title,
  detail,
  note,
  recovery = false,
  children,
}: {
  kind: "empty" | "unavailable";
  icon: NleIconName;
  title: string;
  detail: string;
  note?: string;
  recovery?: boolean;
  children?: ReactNode;
}) {
  return (
    <div
      className="h3-nle-picture-overlay"
      role="status"
      data-h3-nle-overlay={kind}
      data-h3-nle-empty={kind === "empty" ? "timeline" : undefined}
      data-h3-nle-recovery={recovery ? "" : undefined}
    >
      <NleActionIcon name={icon} size={kind === "empty" ? 34 : 26} />
      <p data-h3-nle-overlay-title="">{title}</p>
      <p data-h3-nle-overlay-detail="">{detail}</p>
      {note !== undefined ? <p data-h3-nle-overlay-note="">{note}</p> : null}
      {children}
    </div>
  );
}

export function NleMonitor({
  locale,
  monitor,
  leaseScheduler,
  selectedClip,
  onFrame,
  onTimelineRequest,
  onPresentationMeasurement,
  bindPause,
  bindTimelineTransport,
  bindReplace,
  retainedFrame,
  logicalFrame,
  sourceProjection,
  openSourcePreview,
  emptyTimeline = false,
  output,
  mediaTools,
  overlayGeneration,
  statusChannel,
  transformEditable = false,
  transformAuthority = "",
  onTransformCommit,
}: {
  locale: Locale;
  monitor: NleMonitorBinding;
  leaseScheduler?: NlePlaybackLeaseScheduler;
  selectedClip: CompositionClip | undefined;
  onFrame(frame: number | null): void;
  onTimelineRequest?(
    request: Readonly<{ generation: number; frame: number }> | null,
    settledRequestGeneration: number,
  ): void;
  onPresentationMeasurement?: (sample: PresentationMeasurement) => void;
  bindPause?(pause: (() => Promise<void>) | null): void;
  /** M25-46: the ruler delegates to this already-ordered transport; it owns no second runtime. */
  bindTimelineTransport?(
    bridge: Readonly<{
      seek(frame: number): void;
      pause(): Promise<void>;
      toggle(): void;
      step(direction: -1 | 1): void;
      available: boolean;
    }> | null,
  ): () => void;
  /**
   * M25-21: lets the session owner that adopts an accepted revision start the monitor's
   * replacement in the same task, so the adoption and the monitor's `opening` state are published
   * in one React commit instead of two. Registering is optional; without it the binding effect
   * above still performs the replacement one commit later.
   */
  bindReplace?(
    replace: ((binding: VisualCompositionBinding) => void) | null,
  ): void;
  /** M25-21: the retained position for this composition workspace, restored paused. */
  retainedFrame?: Readonly<{
    restore(): number | null;
    retain(frame: number): void;
  }>;
  /** Workspace-owned logical position; it survives preview failure and drives rebind first paint. */
  logicalFrame?: () => number;
  sourceProjection?: AuthoringProjection;
  openSourcePreview?: AuthoringPreviewOpener;
  /** M25-33: the accepted snapshot has no clips; the monitor points to clip insertion. */
  emptyTimeline?: boolean;
  /**
   * M25-64: the accepted snapshot's output profile, for the header. The composition binding
   * carries the same object, but the fallback modes have no binding and still have a snapshot.
   */
  output?: Readonly<Record<string, unknown>>;
  mediaTools?: MediaToolsBinding;
  overlayGeneration?: number;
  /** M25-45: where the monitor's status and audio state are shown -- the chrome bar. */
  statusChannel?: NleMonitorStatusChannel;
  /** M25-51: one selected, active, enabled clip on an enabled and unlocked track. */
  transformEditable?: boolean;
  /** Accepted timeline identity, plus conflict/error disposition, for draft cancellation. */
  transformAuthority?: string;
  onTransformCommit?(transform: TransformWire): Promise<void> | void;
}) {
  const text = nleCopy(locale);
  const sourcePreviewAuthority =
    sourceProjection === undefined || selectedClip === undefined
      ? null
      : [
          sourceProjection.workspaceHandle,
          sourceProjection.reference.revision,
          sourceProjection.timeline.revision,
          sourceProjection.timeline.contentFingerprint,
          selectedClip.clipId,
          monitor.mode,
        ].join("\u0000");
  const [sourcePreview, setSourcePreview] = useState<string | null>(null);
  const sourcePreviewIntent = useRef<Readonly<{
    clipId: string;
    authority: string;
  }> | null>(null);
  const updateSourcePreview = useCallback(
    (next: string | null) => {
      const current = sourcePreviewIntent.current;
      if (next === null) {
        if (current === null) return;
        sourcePreviewIntent.current = null;
        setSourcePreview(null);
        return;
      }
      if (sourcePreviewAuthority === null) return;
      if (
        current?.clipId === next &&
        current.authority === sourcePreviewAuthority
      )
        return;
      // IMPORTANT (B-M2545-35): bind queued intent to the exact projection and selection before
      // React commits it. Otherwise an Open and a projection replacement in one batch can mount
      // the stale intent against the replacement identity before the invalidation effect runs.
      sourcePreviewIntent.current = {
        clipId: next,
        authority: sourcePreviewAuthority,
      };
      setSourcePreview(next);
    },
    [sourcePreviewAuthority],
  );
  const sourceButton = useRef<HTMLButtonElement>(null);
  const legacyClip = sourceProjection?.timeline.clips.find(
    (clip) => clip.clipId === selectedClip?.clipId,
  );
  useEffect(() => {
    if (
      sourcePreviewIntent.current !== null &&
      sourcePreviewIntent.current.authority !== sourcePreviewAuthority
    )
      updateSourcePreview(null);
  }, [sourcePreviewAuthority, updateSourcePreview]);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const pictureRef = useRef<HTMLDivElement>(null);
  // The picture rectangle and the view it was computed for. The monitor owns both: M25-51 hit
  // tests against this rectangle, and deriving it from the pane instead would include the
  // transport row, the seek bar and the padding.
  const [picture, setPicture] = useState<PictureBox>(EMPTY_PICTURE);
  // The latest measurement, for the session that is created after the first one is taken.
  const pictureRect = useRef<PictureBox>(EMPTY_PICTURE);
  const [view, setView] = useState<PictureView>("fit");
  // The R2 element: the monitor owns it, full screen is requested on it, and its key handler owns
  // the transport shortcuts.
  const sectionRef = useRef<HTMLElement>(null);
  const handleRef = useRef<MonitorHandle | undefined>(undefined);
  const activeBinding = useRef<VisualCompositionBinding | null>(null);
  const listeners = useRef(new Set<() => void>());
  const snapshotRef = useRef<NleMonitorStatus>(CLOSED);
  const onFrameRef = useRef(onFrame);
  onFrameRef.current = onFrame;
  const measurementRef = useRef(onPresentationMeasurement);
  measurementRef.current = onPresentationMeasurement;
  const bindPauseRef = useRef(bindPause);
  bindPauseRef.current = bindPause;
  const retainedFrameRef = useRef(retainedFrame);
  retainedFrameRef.current = retainedFrame;
  // The retained frame being restored; until the monitor presents it (or the user takes the
  // transport) the presented frame is not the user's position and is not retained.
  const restoreTarget = useRef<number | null>(null);
  // IMPORTANT (M25-21 B3-D10): the slider shows, and steps from, the frame the user last sought
  // until that seek settles. Stepping from the presented frame lost every key press that arrived
  // before the previous seek was presented (two quick PageUps moved one second), and React reset
  // the controlled range to the presented frame, collapsing held arrow keys the same way. It is
  // state, not a ref: the controlled input must re-render with the requested value.
  const [requestedFrame, setRequestedFrame] = useState<number | null>(null);
  // IMPORTANT (M25-21 B3-D33): the mount effect's cleanup is created once and would close over the
  // first render's `requestedFrame`. Retention reads the position through this ref instead.
  const requestedFrameRef = useRef(requestedFrame);
  const onTimelineRequestRef = useRef(onTimelineRequest);
  onTimelineRequestRef.current = onTimelineRequest;
  const timelineRequestRef = useRef<
    Readonly<{
      request: Readonly<{ generation: number; frame: number }> | null;
      settledRequestGeneration: number;
    }>
  >({ request: null, settledRequestGeneration: 0 });
  const nextRequestGeneration = useRef(0);
  const publishTimelineRequest = (
    request: Readonly<{ generation: number; frame: number }> | null,
    settledRequestGeneration = timelineRequestRef.current
      .settledRequestGeneration,
  ) => {
    timelineRequestRef.current = { request, settledRequestGeneration };
    const nextFrame = request?.frame ?? null;
    if (requestedFrameRef.current !== nextFrame) {
      requestedFrameRef.current = nextFrame;
      setRequestedFrame(nextFrame);
    }
    onTimelineRequestRef.current?.(request, settledRequestGeneration);
  };
  const settleActiveTimelineRequest = () => {
    const active = timelineRequestRef.current.request;
    if (active === null) return;
    publishTimelineRequest(
      null,
      Math.max(
        timelineRequestRef.current.settledRequestGeneration,
        active.generation,
      ),
    );
  };
  // The seek the user last asked for, while it is still in flight; see `seekTo`.
  const pendingSeek = useRef<Promise<unknown> | null>(null);
  // A Play can be inside the native transport while the last published snapshot is still paused.
  // Scrub pause must sequence behind that promise instead of trusting the stale published state.
  const pendingPlay = useRef<Promise<unknown> | null>(null);
  const timelineSeekRef = useRef<(frame: number) => void>(() => undefined);
  const timelinePauseRef = useRef<() => Promise<void>>(() => Promise.resolve());
  const timelineToggleRef = useRef<() => void>(() => undefined);
  const timelineStepRef = useRef<(direction: -1 | 1) => void>(() => undefined);
  // IMPORTANT: a Play deferred behind seek still belongs to its original user intent. A later
  // pause (including trim), transport action, replacement or teardown must invalidate it before
  // it reaches the runtime, which would otherwise treat the delayed call as a new Play.
  const transportIntent = useRef(0);
  const invalidateTransport = () => {
    const clearRequestedFrame =
      requestedFrameRef.current !== null || pendingSeek.current !== null;
    transportIntent.current++;
    pendingSeek.current = null;
    pendingPlay.current = null;
    restoreTarget.current = null;
    // IMPORTANT: replacement publishes opening through the external store synchronously.
    // Enqueuing a null-to-null update in its fallback effect adds a duplicate commit. A seek
    // queued in this same turn still needs clearing even before its rendered ref catches up.
    if (clearRequestedFrame) settleActiveTimelineRequest();
  };

  // B-M2561-04: a rebind keeps the user's position. A seek in flight keeps its requested frame
  // (the runtime holds it for the new owner), so only a Play deferred behind that seek and a
  // pending restore are invalidated, as a replacement always did.
  const rebindTransport = () => {
    transportIntent.current++;
    pendingPlay.current = null;
    restoreTarget.current = null;
  };

  const status = useSyncExternalStore(
    (listener) => {
      listeners.current.add(listener);
      return () => {
        listeners.current.delete(listener);
      };
    },
    () => snapshotRef.current,
    () => snapshotRef.current,
  );

  // M25-45: the chrome bar shows this monitor's status and audio state, so the picture area keeps
  // the height the two text rows used to take.
  //
  // IMPORTANT: the channel is written inside `publish` below, in the same synchronous callback
  // that notifies this component's own subscribers, never from an effect that mirrors the
  // rendered status afterwards. React batches the two updates into one commit that way; mirroring
  // in an effect instead adds a second commit for every status tick, and an accepted edit went
  // from four commits to six against the frozen budget of four.
  //
  // Publishing `null` when no composition is bound keeps the chrome bar from claiming a state the
  // monitor is not in -- the fallback region says that where the user is looking.
  const statusChannelRef = useRef(statusChannel);
  statusChannelRef.current = statusChannel;
  useEffect(() => {
    if (monitor.mode === "composition") return;
    statusChannel?.publish(null);
  }, [statusChannel, monitor.mode]);

  const binding = monitor.binding;
  const headerFacts = monitorHeaderFacts(
    locale,
    binding?.snapshot.output ?? output,
  );
  const outputWidth = Number(binding?.snapshot.output.width ?? 0);
  const outputHeight = Number(binding?.snapshot.output.height ?? 0);
  const acceptedTransform = transformWire(selectedClip);
  const selectedGeometry =
    selectedClip === undefined
      ? null
      : (handleRef.current?.session.getVisualLayerGeometry?.(
          selectedClip.clipId,
        ) ?? null);
  const selectedActive =
    selectedClip !== undefined &&
    status.frame !== null &&
    status.frame >= selectedClip.startFrame &&
    status.frame < selectedClip.startFrame + selectedClip.durationFrames;
  const transformLayer =
    selectedGeometry === null
      ? null
      : Object.freeze({
          centerX: selectedGeometry.centerX,
          centerY: selectedGeometry.centerY,
          width: selectedGeometry.scaledWidth,
          height: selectedGeometry.scaledHeight,
          rotationMdeg: Math.round(
            (selectedGeometry.angleRadians * 180_000) / Math.PI,
          ),
        });

  /**
   * Measure the picture area and tell the session what it may draw into.
   *
   * IMPORTANT (M25-45): the observed box is the area, and the picture is the largest rectangle of
   * the composition's aspect ratio inside it, so a letterboxed composition does not report the
   * whole pane as picture. A zero or hidden box is not a measurement and the previous one stands,
   * because a collapsed pane -- a closed tab, a splitter dragged shut -- would otherwise throw the
   * backing store away and force a repaint when it reopens. The device pixel ratio is observed as
   * well as the CSS size: moving the window to a different display changes how many device pixels
   * the same rectangle holds, and nothing else fires then.
   */
  useEffect(() => {
    const area = pictureRef.current;
    if (area === null || outputWidth <= 0 || outputHeight <= 0) return;
    let ratioQuery: MediaQueryList | null = null;
    const apply = () => {
      const box = area.getBoundingClientRect();
      if (box.width <= 0 || box.height <= 0) return;
      const next = computePictureBox(
        box.width,
        box.height,
        outputWidth,
        outputHeight,
        view,
      );
      pictureRect.current = next;
      setPicture((current) =>
        current.width === next.width &&
        current.height === next.height &&
        current.left === next.left &&
        current.top === next.top
          ? current
          : next,
      );
      handleRef.current?.session.resize({
        cssWidth: next.width,
        cssHeight: next.height,
        devicePixelRatio: window.devicePixelRatio,
      });
      if (typeof window.matchMedia !== "function") return;
      const ratio = window.devicePixelRatio;
      ratioQuery?.removeEventListener("change", apply);
      ratioQuery = window.matchMedia(`(resolution: ${ratio}dppx)`);
      ratioQuery.addEventListener("change", apply);
    };
    apply();
    // The same capability guard the shell and the timeline use: an environment without the
    // observer keeps the one measurement above rather than losing the picture entirely.
    if (typeof ResizeObserver === "undefined")
      return () => ratioQuery?.removeEventListener("change", apply);
    const observer = new ResizeObserver(apply);
    observer.observe(area);
    return () => {
      observer.disconnect();
      ratioQuery?.removeEventListener("change", apply);
    };
  }, [outputWidth, outputHeight, view]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null || binding === null) return;
    const handle = createNleMonitor(canvas, binding, {
      ...(leaseScheduler === undefined
        ? {}
        : { playbackScheduler: leaseScheduler }),
      onPresentationMeasurement: (sample) => measurementRef.current?.(sample),
    });
    handleRef.current = handle;
    // The picture area is measured before this effect runs, so the session is told its box at
    // creation rather than waiting for the next observation -- otherwise the first frame is
    // presented at the floor and repainted a moment later.
    if (pictureRect.current.width > 0)
      handle.session.resize({
        cssWidth: pictureRect.current.width,
        cssHeight: pictureRect.current.height,
        devicePixelRatio: window.devicePixelRatio,
      });
    bindPauseRef.current?.(() => {
      invalidateTransport();
      return handle.session.pause();
    });
    activeBinding.current = binding;
    const lastFrame = Number(binding.snapshot.output.durationFrames) - 1;
    const retained = retainedFrameRef.current?.restore() ?? null;
    restoreTarget.current =
      retained !== null && retained > 0 && lastFrame > 0
        ? Math.min(retained, lastFrame)
        : null;
    const publish = () => {
      const next = handle.status();
      snapshotRef.current = next;
      if (next.frame !== null && next.frame === restoreTarget.current)
        restoreTarget.current = null;
      onFrameRef.current(next.frame);
      statusChannelRef.current?.publish(next);
      for (const listener of listeners.current) listener();
    };
    const unsubscribe = handle.subscribe(publish);
    publish();
    // IMPORTANT: on a remount the playhead channel is freshly constructed at frame 0, while the
    // retained target is the last user-owned position. Letting logicalFrame win here silently
    // discards both a paused position and an in-flight user seek; replace/recover still use the
    // live logical frame below because those operations keep the existing workspace channel.
    ignore(
      handle.session.open(() =>
        Math.max(
          0,
          Math.min(lastFrame, restoreTarget.current ?? logicalFrame?.() ?? 0),
        ),
      ),
    );
    return () => {
      // Keep the user's position before teardown clears it. A restore still in flight means
      // the presented frame is not theirs yet, so the earlier retained position stands.
      // IMPORTANT (M25-21 B3-D33): a seek the user has made but the media has not presented yet
      // is still their position -- the transport already shows it (B3-D10) -- so the requested
      // frame wins over the presented one. Retaining only the presented frame lost every seek
      // made within a media fetch of the release: the user sought a second in, collapsed the
      // view, and the monitor came back at the frame the seek had moved away from.
      const presented = requestedFrameRef.current ?? snapshotRef.current.frame;
      if (presented !== null && restoreTarget.current === null)
        retainedFrameRef.current?.retain(presented);
      transportIntent.current++;
      pendingSeek.current = null;
      pendingPlay.current = null;
      unsubscribe();
      handleRef.current = undefined;
      bindPauseRef.current?.(null);
      activeBinding.current = null;
      snapshotRef.current = CLOSED;
      statusChannelRef.current?.publish(null);
      onFrameRef.current(null);
      // IMPORTANT: React cleanup cannot await; observe the owned close so a rejected
      // release cannot escape as an unhandled rejection after unmount.
      ignore(handle.dispose());
    };
    // The session is created once per mount; binding changes go through replace below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [binding === null, leaseScheduler]);

  // IMPORTANT (M25-21 B3-D56): this effect is the fallback, not the only path. Reacting to the
  // binding prop means the replacement can only start *after* the commit that adopted the accepted
  // revision, so the monitor's `opening` state is necessarily a second React commit. The session
  // owner that adopts the revision therefore starts the replacement itself, through the trigger
  // registered below, before it re-renders -- the two truthful states are published together and
  // the actual replacement stays asynchronous. This effect still runs and is still correct: the
  // trigger records the binding it started in the same ref, so `sameMonitorBinding` skips it. Do
  // not move the trigger into render to save the effect; a replacement started during render is a
  // render-time side effect and can be discarded by a re-render that never commits.
  useEffect(() => {
    const handle = handleRef.current;
    if (handle === undefined || binding === null) return;
    if (sameMonitorBinding(activeBinding.current, binding)) return;
    rebindTransport();
    activeBinding.current = binding;
    ignore(handle.replace(binding, () => logicalFrame?.() ?? 0));
  }, [binding, logicalFrame]);

  useEffect(() => {
    if (bindReplace === undefined) return;
    bindReplace((next) => {
      const handle = handleRef.current;
      if (handle === undefined) return;
      if (sameMonitorBinding(activeBinding.current, next)) return;
      rebindTransport();
      activeBinding.current = next;
      ignore(handle.replace(next, () => logicalFrame?.() ?? 0));
    });
    return () => bindReplace(null);
  }, [bindReplace, logicalFrame]);

  const composition = status.composition;
  // B-M2561-04: a rebind to an accepted revision keeps the transport available; the runtime
  // holds what the controls send until the new owner is ready.
  const controlsAvailable =
    composition.status === "paused" ||
    composition.status === "playing" ||
    status.rebinding === true;
  const recoveryAvailable =
    composition.status === "blocked" ||
    composition.blocker === "cleanup_pending";
  const lastFrame = Math.max(0, composition.durationFrames - 1);
  // One output second under the accepted rational frame rate (24/1 on the selected profile).
  const frameRate = binding?.snapshot.output.frameRate as
    Readonly<{ num: number; den: number }> | undefined;
  const pageFrames =
    frameRate !== undefined && frameRate.num >= 1 && frameRate.den >= 1
      ? Math.max(1, Math.round(frameRate.num / frameRate.den))
      : 1;
  const unavailableReason = monitorUnavailableReason(text, composition);
  const positionFrame = requestedFrame ?? status.frame;
  // Every caller is a user transport control; taking the transport ends a pending restore and
  // any requested seek position.
  const session = () => {
    transportIntent.current++;
    restoreTarget.current = null;
    settleActiveTimelineRequest();
    return handleRef.current?.session;
  };
  const seekTo = (frame: number) => {
    const active = session();
    if (!active) return;
    const target = Math.min(lastFrame, Math.max(0, frame));
    const request = {
      generation: ++nextRequestGeneration.current,
      frame: target,
    } as const;
    publishTimelineRequest(request);
    // The user can seek away and back to the same frame before either request finishes. Only
    // the latest request may settle the slider; equal frame values do not identify that request.
    const settle = () => {
      if (pendingSeek.current === task)
        publishTimelineRequest(null, request.generation);
    };
    const task = active.seek(target).then(settle, settle);
    // IMPORTANT (M25-21 B3-D29): the user's next transport intent has to queue behind this seek.
    // Play judges the endpoint by the frame actually presented, so a Play issued while a seek
    // away from the last frame is still in flight is refused as "already at the end" and the
    // session silently republishes `paused` -- the button does nothing until it is pressed a
    // second time. "Home, then Play" at the end of a composition is exactly that sequence. The
    // ordering belongs here, where the two user intents are sequenced: the runtime deliberately
    // supersedes rather than serializes, and making it wait instead breaks that model.
    pendingSeek.current = task;
    void task.finally(() => {
      if (pendingSeek.current === task) pendingSeek.current = null;
    });
  };
  // IMPORTANT (M25-44 B-M2544-05): a frame step while a requested seek is still unpresented steps
  // from that seek, as the slider's arrow keys do (B3-D10). The session's step pauses first, and a
  // pause with a move in flight supersedes it with a seek back to the last presented frame, so
  // handing this case to `session.step` cancels the user's seek: Home, then Next frame, landed on
  // the old frame plus one. Playback owns no requested frame and keeps the pause-and-step.
  const stepBy = (direction: -1 | 1) => {
    if (requestedFrame !== null && composition.status === "paused") {
      const target = Math.min(
        lastFrame,
        Math.max(0, requestedFrame + direction),
      );
      if (target !== requestedFrame) seekTo(target);
      return;
    }
    const active = session();
    if (active) ignore(active.step(direction));
  };

  // The play the button and the Space key share: a Play issued while a seek is still in flight
  // waits for it, and is abandoned if anything else takes the transport meanwhile.
  const playNow = () => {
    const active = session();
    if (!active) return;
    const queued = pendingSeek.current;
    const intent = transportIntent.current;
    const playIfCurrent = () => {
      if (
        intent === transportIntent.current &&
        handleRef.current?.session === active
      )
        return active.play();
    };
    const task = Promise.resolve(
      queued === null
        ? active.play()
        : queued.then(playIfCurrent, playIfCurrent),
    );
    pendingPlay.current = task;
    ignore(task);
    void task.then(
      () => {
        if (pendingPlay.current === task) pendingPlay.current = null;
      },
      () => {
        if (pendingPlay.current === task) pendingPlay.current = null;
      },
    );
  };
  const pauseNow = (): Promise<void> => {
    const queuedPlay = pendingPlay.current;
    const publishedStatus = snapshotRef.current.composition.status;
    // IMPORTANT: a ruler key can arrive while the previous seek is still settling. Pausing a truly
    // paused owner increments the transport intent and cancels that seek. A pending Play is the
    // exception: the published status remains paused until native play settles, so wait for that
    // exact intent and pause it before seeking. Otherwise a scrub inherits playback, a late RVFC
    // replaces an exact observed PTS, and the healthy monitor latches source_unavailable.
    if (publishedStatus === "paused" && queuedPlay === null)
      return Promise.resolve();
    const active = session();
    if (!active) return Promise.resolve();
    // Once playing is published the native Play has settled, even if the bookkeeping microtask
    // has not cleared `pendingPlay` yet. Pause synchronously like the accepted transport control.
    if (publishedStatus !== "paused" || queuedPlay === null)
      return active.pause();
    const pauseIfCurrent = () => {
      if (handleRef.current?.session === active) return active.pause();
    };
    return queuedPlay
      .then(pauseIfCurrent, pauseIfCurrent)
      .then(() => undefined);
  };
  timelineSeekRef.current = seekTo;
  timelinePauseRef.current = pauseNow;
  timelineToggleRef.current = () => {
    if (snapshotRef.current.composition.status === "playing") void pauseNow();
    else playNow();
  };
  timelineStepRef.current = stepBy;

  useEffect(() => {
    // CRITICAL: keep this hook above every mode-specific return. A loading-to-composition switch
    // otherwise changes the hook count and React tears down the accepted monitor session.
    if (bindTimelineTransport === undefined) return;
    if (monitor.mode !== "composition") return bindTimelineTransport(null);
    // CRITICAL: keep this bridge stable across frame/status renders. Rebinding cleans the owner's
    // in-flight scrub pause; the following seek then inherits playback and a late RVFC can replace
    // the exact observed PTS, latching source_unavailable. Refs preserve the newest handlers.
    return bindTimelineTransport({
      seek: (frame) => timelineSeekRef.current(frame),
      pause: () => timelinePauseRef.current(),
      toggle: () => timelineToggleRef.current(),
      step: (direction) => timelineStepRef.current(direction),
      available: controlsAvailable,
    });
  }, [bindTimelineTransport, monitor.mode, controlsAvailable]);

  if (monitor.mode !== "composition") {
    const sourceOnly = monitor.mode === "selected_source_only";
    return (
      <section className="h3-nle-monitor" aria-label={text.monitor.title}>
        <MonitorHeader text={text} facts={headerFacts} />
        <div
          className="h3-nle-picture h3-nle-picture-fallback"
          role="region"
          aria-label={text.monitor.selectedSourceOnly}
          data-h3-nle-fallback="selected_source_only"
          data-h3-nle-status="monitor"
        >
          {/* M25-33: no clip is not a monitor or runtime failure; say what to do instead. */}
          {emptyTimeline ? (
            <PictureOverlay
              kind="empty"
              icon="preview"
              title={text.monitor.emptyTitle}
              detail={text.monitor.emptyHint}
            />
          ) : sourceOnly &&
            legacyClip &&
            sourceProjection &&
            openSourcePreview ? (
            <>
              <PictureOverlay
                kind="unavailable"
                icon="warning"
                title={text.monitor.unavailableTitle}
                detail={text.surface.fallback}
              >
                <button
                  data-h3-plain
                  ref={sourceButton}
                  type="button"
                  onClick={() => updateSourcePreview(legacyClip.clipId)}
                >
                  {text.monitor.selectedSourceOnly}
                </button>
              </PictureOverlay>
              {sourcePreview === legacyClip.clipId &&
              sourcePreviewIntent.current?.authority ===
                sourcePreviewAuthority ? (
                <AuthoringPreviewMonitor
                  locale={locale}
                  identity={{
                    workspaceHandle: sourceProjection.workspaceHandle,
                    referenceRevision: sourceProjection.reference.revision,
                    timelineRevision: sourceProjection.timeline.revision,
                    timelineContentFingerprint:
                      sourceProjection.timeline.contentFingerprint,
                    clipId: legacyClip.clipId,
                    startFrame: legacyClip.startFrame,
                    frames: legacyClip.frames,
                    sourceStartFrame: legacyClip.sourceStartFrame,
                    fps: sourceProjection.timeline.profile.videoFps,
                  }}
                  openPreview={openSourcePreview}
                  returnFocus={sourceButton.current}
                  onClose={() => updateSourcePreview(null)}
                  mediaTools={mediaTools}
                  mediaPlacement="contextual-overlay"
                  overlayGeneration={overlayGeneration ?? null}
                />
              ) : null}
            </>
          ) : (
            // The raw asset id the old note printed is gone (R5); the source preview's own
            // button above is how a selected source is named and opened.
            <PictureOverlay
              kind="unavailable"
              icon="warning"
              title={text.monitor.unavailableTitle}
              detail={
                sourceOnly
                  ? text.surface.fallback
                  : text.surface.monitorUnavailable
              }
              note={
                sourceOnly && selectedClip === undefined
                  ? text.monitor.selectedSourceNone
                  : undefined
              }
            />
          )}
        </div>
      </section>
    );
  }

  /**
   * The monitor's own keys: Space toggles playback, comma and period step, and the arrows step
   * while focus is inside the monitor.
   *
   * IMPORTANT: they are owned here, on the monitor, and every consumed key is stopped. A supplied
   * host binds page-wide hotkeys on `document` without exempting the modal (M26-05 runtime-18), so
   * a key this transport acts on must not also reach the host. Editable fields and focused buttons
   * keep their own behaviour: Space on a focused button already activates it, and handling it here
   * as well plays and pauses in the same press. The seek range stops its own arrows before they
   * reach this handler.
   */
  const onKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target as HTMLElement | null;
    if (
      target !== null &&
      (target.isContentEditable ||
        target.closest("input:not([type='range']),textarea,select") !== null)
    )
      return;
    if (event.key === " " || event.key === "Spacebar") {
      if (target !== null && target.closest("button,a,summary") !== null)
        return;
      if (!controlsAvailable) return;
      event.preventDefault();
      event.stopPropagation();
      if (composition.status === "playing") pauseNow();
      else playNow();
      return;
    }
    const step =
      event.key === "," || event.key === "ArrowLeft"
        ? -1
        : event.key === "." || event.key === "ArrowRight"
          ? 1
          : null;
    if (step === null || !controlsAvailable) return;
    event.preventDefault();
    event.stopPropagation();
    stepBy(step);
  };

  return (
    <section
      className="h3-nle-monitor"
      aria-label={text.monitor.title}
      ref={sectionRef}
      // IMPORTANT: the timeline ruler replaced the monitor's focusable range. Keep one neutral
      // monitor focus target or the accepted Space/comma/period shortcuts become unreachable.
      tabIndex={0}
      data-h3-nle-view={view}
      onKeyDown={onKeyDown}
    >
      <MonitorHeader text={text} facts={headerFacts} />
      {/* The area carries the composition size it is fitting, so an observer can check the fit
          rule against the composition rather than against the picture it is measuring. */}
      <div
        className="h3-nle-picture"
        ref={pictureRef}
        data-h3-nle-picture={`${outputWidth}x${outputHeight}`}
      >
        <canvas
          ref={canvasRef}
          data-h3-nle-canvas="composition"
          // B-M2564-12: the frame on the canvas, which observers wait on before they read its
          // pixels -- not the transport's kept frame, which it trails during a rebind.
          data-h3-nle-presented-frame={status.presentedFrame ?? undefined}
          role="img"
          aria-label={text.monitor.canvas}
          // IMPORTANT (M25-61): the canvas sits at the box's own whole-pixel offsets. Left to the
          // area's flex centring it lands on a half pixel whenever the leftover height or width
          // is odd, while the transform overlay is placed from the rounded box, so the two
          // disagree by 0.5 px -- 14 bp of a 359 px picture -- and every alignment button misses.
          style={
            picture.width > 0
              ? {
                  position: "absolute",
                  left: `${picture.left}px`,
                  top: `${picture.top}px`,
                  width: `${picture.width}px`,
                  height: `${picture.height}px`,
                }
              : undefined
          }
        />
        {selectedClip !== undefined &&
        selectedActive &&
        transformLayer !== null &&
        picture.width > 0 &&
        outputWidth > 0 &&
        outputHeight > 0 &&
        onTransformCommit !== undefined ? (
          <NleTransformOverlay
            locale={locale}
            clipId={selectedClip.clipId}
            authority={transformAuthority}
            accepted={acceptedTransform}
            layer={transformLayer}
            picture={{
              left: picture.left,
              top: picture.top,
              width: picture.width,
              height: picture.height,
              outputWidth,
              outputHeight,
            }}
            disabled={!transformEditable || composition.status === "blocked"}
            onBegin={pauseNow}
            onPreview={(transform) => {
              const geometry =
                handleRef.current?.session.previewVisualTransform?.(
                  selectedClip.clipId,
                  transform,
                ) ?? null;
              if (geometry === null) return null;
              return {
                centerX: geometry.centerX,
                centerY: geometry.centerY,
                width: geometry.scaledWidth,
                height: geometry.scaledHeight,
                rotationMdeg: Math.round(
                  (geometry.angleRadians * 180_000) / Math.PI,
                ),
              };
            }}
            onCommit={onTransformCommit}
          />
        ) : null}
        {/* Recovery belongs where the user is looking when the picture is gone, and only then:
            a permanently rendered, permanently disabled Retry reads as a broken control rather
            than as an offer that applies to one state. It outranks the empty-timeline overlay,
            which offers nothing to do in the monitor. M25-33: no clip is not a monitor or runtime
            failure, so an empty timeline says what to do instead. */}
        {recoveryAvailable ? (
          <PictureOverlay
            kind="unavailable"
            icon="warning"
            title={text.monitor.unavailableTitle}
            detail={unavailableReason}
            recovery
          >
            <button
              data-h3-plain
              type="button"
              data-h3-nle-control="transport.recover"
              onClick={() => {
                const active = session();
                if (active) ignore(active.recover(() => logicalFrame?.() ?? 0));
              }}
            >
              {text.monitor.recover}
            </button>
          </PictureOverlay>
        ) : emptyTimeline ? (
          <PictureOverlay
            kind="empty"
            icon="preview"
            title={text.monitor.emptyTitle}
            detail={text.monitor.emptyHint}
          />
        ) : null}
      </div>
      <NleTransport
        locale={locale}
        playing={composition.status === "playing"}
        controlsAvailable={controlsAvailable}
        currentFrame={positionFrame}
        lastFrame={lastFrame}
        fps={pageFrames}
        view={view}
        onView={setView}
        onPlay={playNow}
        onPause={pauseNow}
        onStep={stepBy}
        fullscreenTarget={sectionRef}
      />
    </section>
  );
}
