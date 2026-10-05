// M25-16 workspace body, laid out since M25-44 in the four regions of the reference shell: the
// media bin (Media and Sequence tabs), the preview monitor, the inspector and the full-width
// timeline. Everything reads the accepted Authoring/Production projections through the binding;
// nothing here holds a second timeline model. The final-render card lives behind the chrome bar's
// Export button (`NleExportMenu`).

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import type {
  AuthoringIntent,
  AuthoringViewState,
} from "../../state/authoringViewState";
import { ImportToEditor } from "../ProductionWorkbench";
import { useRetainedSlot } from "../useRetainedSlot";
import type { TimelineHistoryProjectionV2 } from "../../contracts/authoringWorkbenchCodec";
import type { CompositionClip } from "../../contracts/compositionCodec";
import { canonicalPublicRuntimeAssetFingerprint } from "../../contracts/authoringMediaLeaseCodec";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaPreparedPlayback,
} from "../../host/authoringMediaSourceLease";
import { createNleDecorationCache } from "../../host/nleDecorationCache";
import {
  createNleLeaseScheduler,
  type NleLeaseSchedulerEvent,
} from "../../host/nleLeaseScheduler";
import {
  createNleDecorationDemandBroker,
  type NleRoutedDecorationValue,
} from "../../host/nleDecorationDemandBroker";
import type { OverlayInternalPane } from "../../runtime/nleOverlayGeometry";
import { deriveNleMonitorBinding } from "../../runtime/nleWorkspaceRuntime";
import { createNleBinInsertChannel } from "../../runtime/nleBinInsertChannel";
import { isNleSchedulerTraceEnabled } from "../../runtime/nleWaveformPainter";
import type { PresentationMeasurement } from "../../runtime/visualCompositionSession";
import { buildPublicAssetManifest } from "../../runtime/publicAssetManifest";
import { saveIndicatorModel } from "../../runtime/nleSaveIndicator";
import { formatTimelineTimecode } from "../../runtime/timelineNavigation";
import {
  NleActionIcon,
  NleIconButton,
  NleIconGroup,
  type NleIconName,
} from "./NleIconActions";
import { NleInspectorTabs } from "./NleInspectorTabs";
import { NleAuthoringAssetBin } from "./NleAuthoringAssetBin";
import { NleEmptyTimelineDrop } from "./NleEmptyTimelineDrop";
import { NleMediaBin } from "./NleMediaBin";
import { NleMonitor } from "./NleMonitor";
import { NleSequencePanel } from "./NleSequencePanel";
import { NleShell } from "./NleShell";
import {
  NleTimeline,
  type NleTimelineProps,
  type NleTimelineShortcutHandler,
} from "./NleTimeline";
import {
  decideTimelineHistoryTool,
  type TimelineRebaseAttempt,
} from "./nleTimelineTools";
import { NleTextBin } from "./NleTextBin";
import { fill, nleCopy } from "./nleCopy";
import type { NleMonitorStatusChannel } from "./nleMonitorChannel";
import type { NleWorkspaceBinding } from "./nleWorkspaceBinding";
import { build, type TransformWire } from "./nleCommandBuilders";
import { useNleAssetPreparation } from "./useNleAssetPreparation";

function EmptyV2TimelineHistory({
  locale,
  status,
  history,
  onIntent,
}: Readonly<{
  locale: Parameters<typeof nleCopy>[0];
  status: AuthoringViewState["status"];
  history: TimelineHistoryProjectionV2;
  onIntent(intent: AuthoringIntent): Promise<void>;
}>) {
  const toolbar = nleCopy(locale).timeline.toolbar;
  const decisionFor = (action: "undo" | "redo") =>
    decideTimelineHistoryTool(action, {
      authoringStatus: status,
      undoCursor: history.undoCursor,
      redoCursor: history.redoCursor,
    });
  const undo = decisionFor("undo");
  const redo = decisionFor("redo");
  const description = (
    decision: ReturnType<typeof decideTimelineHistoryTool>,
    action: "undo" | "redo",
  ) => {
    return decision.enabled || decision.reason === null
      ? toolbar.descriptions[action]
      : toolbar.unavailable[decision.reason];
  };
  const submit = (
    action: "undo" | "redo",
    decision: ReturnType<typeof decideTimelineHistoryTool>,
  ) => {
    if (!decision.enabled) return;
    const authoring = history.authoring;
    void onIntent({
      action: "apply_timeline_commands",
      capturedTimeline: {
        workspaceHandle: authoring.workspaceHandle,
        workspaceRevision: authoring.workspaceRevision,
        timelineRevision: authoring.timelineRevision,
        timelineFingerprint: authoring.timelineFingerprint,
        authoringFingerprint: authoring.authoringFingerprint,
      },
      commands: decision.commands,
    });
  };
  return (
    <div className="h3-nle-timeline-toolbar-stack">
      <div
        className="h3-nle-timeline-toolbar"
        role="toolbar"
        aria-label={toolbar.label}
      >
        <NleIconGroup label={toolbar.label} control="timeline.empty-history">
          <NleIconButton
            icon="undo"
            label={toolbar.labels.undo}
            description={description(undo, "undo")}
            hue="edit"
            control="history.undo"
            disabled={!undo.enabled}
            focusableWhenDisabled
            toolbarItem
            onActivate={() => submit("undo", undo)}
          />
          <NleIconButton
            icon="redo"
            label={toolbar.labels.redo}
            description={description(redo, "redo")}
            hue="edit"
            control="history.redo"
            disabled={!redo.enabled}
            focusableWhenDisabled
            toolbarItem
            onActivate={() => submit("redo", redo)}
          />
        </NleIconGroup>
      </div>
    </div>
  );
}

function createPlayheadChannel() {
  let state: Readonly<{
    frame: number | null;
    request: Readonly<{ generation: number; frame: number }> | null;
    settledRequestGeneration: number;
    transportAvailable: boolean;
    navigationAvailable: boolean;
  }> = {
    frame: 0,
    request: null,
    settledRequestGeneration: 0,
    transportAvailable: false,
    navigationAvailable: true,
  };
  const listeners = new Set<() => void>();
  const update = (
    next: Readonly<{
      frame: number | null;
      request: Readonly<{ generation: number; frame: number }> | null;
      settledRequestGeneration: number;
      transportAvailable: boolean;
      navigationAvailable: boolean;
    }>,
  ) => {
    if (
      state.frame === next.frame &&
      state.request?.generation === next.request?.generation &&
      state.request?.frame === next.request?.frame &&
      state.settledRequestGeneration === next.settledRequestGeneration &&
      state.transportAvailable === next.transportAvailable &&
      state.navigationAvailable === next.navigationAvailable
    )
      return;
    state = next;
    for (const listener of listeners) listener();
  };
  return {
    snapshot: () => state,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    publish: (frame: number | null) => update({ ...state, frame }),
    publishTimelineRequest: (
      request: Readonly<{ generation: number; frame: number }> | null,
      settledRequestGeneration: number,
    ) => update({ ...state, request, settledRequestGeneration }),
    publishAvailability: (transportAvailable: boolean) =>
      update({ ...state, transportAvailable }),
  };
}

function TransportTimeline({
  channel,
  ...props
}: Omit<NleTimelineProps, "playheadFrame"> & {
  channel: ReturnType<typeof createPlayheadChannel>;
}) {
  const transport = channel.snapshot();
  return (
    <NleTimeline
      {...props}
      playheadFrame={transport.frame}
      transportAvailable={transport.transportAvailable}
      navigationAvailable={transport.navigationAvailable}
      transportChannel={channel}
    />
  );
}

const BIN_TABS: readonly (readonly [OverlayInternalPane, NleIconName])[] = [
  ["assets", "media"],
  ["text", "text"],
  ["sequence", "storyboard"],
];

export function NleWorkspace({
  binding,
  onEdgeGestureActive,
  monitorStatus,
  onPresentationMeasurement,
}: {
  binding: NleWorkspaceBinding;
  onEdgeGestureActive(active: boolean): void;
  /** M25-45: where the monitor publishes its status for the chrome bar. */
  monitorStatus?: NleMonitorStatusChannel;
  /** @internal M25-45 AC45-04: one sample per presented frame, for the hardening harness. */
  onPresentationMeasurement?: (sample: PresentationMeasurement) => void;
}) {
  const { locale, state, authoring, runtime, leaseClient, actions } = binding;
  const text = nleCopy(locale);
  const projection =
    "projection" in authoring ? authoring.projection : undefined;
  const history =
    "timelineHistory" in authoring ? authoring.timelineHistory : undefined;
  const historyV2 =
    "timelineHistoryV2" in authoring ? authoring.timelineHistoryV2 : undefined;
  const receiptV1 =
    "lastTimelineReceipt" in authoring
      ? authoring.lastTimelineReceipt
      : undefined;
  const receiptV2 =
    "lastTimelineReceiptV2" in authoring
      ? authoring.lastTimelineReceiptV2
      : undefined;
  const authoringV2 = historyV2?.authoring ?? receiptV2?.authoring;
  const authoringFingerprint = authoringV2?.authoringFingerprint;
  // A V2 null render snapshot is authoritative for empty authoring. Do not resurrect a
  // preceding V1/V2 snapshot from a retained receipt while that accepted state is empty.
  const snapshot =
    historyV2 !== undefined
      ? (historyV2.renderSnapshot ?? undefined)
      : receiptV2 !== undefined
        ? (receiptV2.renderSnapshot ?? undefined)
        : (history?.snapshot ?? receiptV1?.snapshot);
  const mediaDecorationCache = useMemo(() => createNleDecorationCache(), []);
  const mediaCacheAuthority = useRef<Readonly<{
    workspaceHandle: string;
    runtimeEpoch: number;
  }> | null>(null);
  useLayoutEffect(() => {
    if (authoringV2 !== undefined || snapshot === undefined) {
      const previous = mediaCacheAuthority.current;
      if (previous !== null) {
        mediaDecorationCache.purgeWorkspace(previous.workspaceHandle);
        mediaCacheAuthority.current = null;
      }
      return;
    }
    const workspaceHandle = snapshot.workspaceHandle;
    const runtimeEpoch = state.surface.generation;
    const previous = mediaCacheAuthority.current;
    if (previous !== null && previous.workspaceHandle !== workspaceHandle)
      mediaDecorationCache.purgeWorkspace(previous.workspaceHandle);
    else if (previous !== null && previous.runtimeEpoch !== runtimeEpoch)
      mediaDecorationCache.purgeWorkspace(workspaceHandle);
    const manifest = buildPublicAssetManifest(snapshot);
    mediaDecorationCache.retainWorkspaceAssets(
      workspaceHandle,
      new Set(manifest.assets.map(canonicalPublicRuntimeAssetFingerprint)),
    );
    mediaCacheAuthority.current = { workspaceHandle, runtimeEpoch };
  }, [authoringV2, mediaDecorationCache, snapshot, state.surface.generation]);
  const receipt = receiptV1;
  // IMPORTANT: an accepted transaction drops obsolete history before its refresh.
  // Its receipt already owns the new snapshot. Keep the monitor on that snapshot
  // or every edit unmounts and aborts native opening, leaving the next lease busy.
  const monitorSnapshot = snapshot;
  const selection = historyV2?.selection ?? history?.selection ?? [];
  const timelineFingerprint =
    authoringV2?.timelineFingerprint ?? snapshot?.timelineFingerprint;
  const historyRejection =
    historyV2 !== undefined
      ? historyV2.rejection
      : (history?.rejection ?? null);
  const timelineAttempt = useRef<TimelineRebaseAttempt | null>(null);
  // IMPORTANT: every edit surface, including both bins, must use this capture seam. Bypassing it
  // lets a later nonrebasable conflict retain and offer an unrelated older rejected attribute edit.
  const timelineIntent = useCallback(
    async (intent: AuthoringIntent) => {
      const dispatchIntent =
        intent.action === "apply_timeline_commands" &&
        intent.capturedTimeline === undefined &&
        authoringV2 !== undefined
          ? {
              ...intent,
              capturedTimeline: {
                workspaceHandle: authoringV2.workspaceHandle,
                workspaceRevision: authoringV2.workspaceRevision,
                timelineRevision: authoringV2.timelineRevision,
                timelineFingerprint: authoringV2.timelineFingerprint,
                authoringFingerprint: authoringV2.authoringFingerprint,
              },
            }
          : intent;
      if (
        intent.action === "apply_timeline_commands" &&
        snapshot !== undefined
      ) {
        if (
          intent.commands.some(
            (command) => command.kind === "rebase_transaction",
          )
        ) {
          // IMPORTANT: one explicit rebase consumes its saved attempt before dispatch. Keeping it
          // after a second conflict would offer the same stale edit again as an unrelated retry.
          timelineAttempt.current = null;
        } else if (
          intent.commands.some((command) => command.kind === "select_clips")
        ) {
          // IMPORTANT: selection is a fresh navigation intent in this UI, not a rejected property
          // edit. Saving it enables Rebase after a stale click and can replay an obsolete selection.
          timelineAttempt.current = null;
        } else
          timelineAttempt.current = Object.freeze({
            baseTimelineFingerprint:
              intent.capturedTimeline?.timelineFingerprint ??
              timelineFingerprint ??
              snapshot.timelineFingerprint,
            commands: Object.freeze([...intent.commands]),
          });
      }
      await actions.timeline(dispatchIntent);
    },
    [actions, authoringV2, snapshot],
  );
  // IMPORTANT: clear only after a successful projection advances beyond the captured base. An
  // eager clear on the dispatching ready render erases the exact commands before a conflict can
  // expose Rebase; keeping an accepted old attempt would rebase unrelated later conflicts.
  useEffect(() => {
    const attempt = timelineAttempt.current;
    if (
      authoring.status === "ready" &&
      timelineFingerprint !== undefined &&
      attempt !== null &&
      attempt.baseTimelineFingerprint !== timelineFingerprint
    )
      timelineAttempt.current = null;
  }, [authoring.status, timelineFingerprint]);
  // Transport frames belong to the timeline slice. Routing them through workspace
  // state rerenders the inspector, asset bin and sequence pane on every media tick.
  const playhead = useMemo(createPlayheadChannel, []);
  const logicalFrame = useCallback(
    () => playhead.snapshot().frame ?? 0,
    [playhead],
  );
  const binInsert = useMemo(createNleBinInsertChannel, []);
  const leaseScheduler = useMemo(() => {
    const diagnosticsEnabled = isNleSchedulerTraceEnabled();
    return createNleLeaseScheduler<
      NleRoutedDecorationValue<
        | AuthoringMediaAssetDecoration["value"]
        | AuthoringMediaPreparedPlayback["value"]
      >
    >(
      diagnosticsEnabled
        ? {
            onDiagnostic(event: NleLeaseSchedulerEvent) {
              const target = window as Window & {
                __h3ContextNleSchedulerTrace?: NleLeaseSchedulerEvent[];
              };
              const trace = (target.__h3ContextNleSchedulerTrace ??= []);
              trace.push(event);
              if (trace.length > 5_000) trace.splice(0, trace.length - 5_000);
            },
          }
        : {},
    );
  }, []);
  const decorationDemandBroker = useMemo(
    () => createNleDecorationDemandBroker(leaseScheduler),
    [leaseScheduler],
  );
  useEffect(
    () => () => {
      // IMPORTANT: unregister all decoration producers before closing the shared playback pool;
      // no consumer may replace another producer's queue through the raw scheduler.
      decorationDemandBroker.close();
      leaseScheduler.close();
      mediaDecorationCache.close();
    },
    [decorationDemandBroker, leaseScheduler, mediaDecorationCache],
  );
  // The catalog's video assets are prepared for playback through the same broker, behind every
  // decoration, so that playback still preempts them and they never hold a thumbnail back.
  useNleAssetPreparation({
    authoring: authoringV2,
    runtimeEpoch: state.surface.generation,
    leaseClient,
    decorationDemandBroker,
  });
  const pauseMonitor = useRef<(() => Promise<void>) | null>(null);
  const timelineTransport = useRef<Readonly<{
    seek(frame: number): void;
    pause(): Promise<void>;
    toggle(): void;
    step(direction: -1 | 1): void;
    available: boolean;
  }> | null>(null);
  const timelineShortcuts = useRef<NleTimelineShortcutHandler | null>(null);
  const bindTimelineShortcuts = useCallback(
    (handler: NleTimelineShortcutHandler | null) => {
      timelineShortcuts.current = handler;
      return () => {
        if (timelineShortcuts.current === handler)
          timelineShortcuts.current = null;
      };
    },
    [],
  );
  const ownWorkspaceShortcut = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) =>
      timelineShortcuts.current?.(
        event as unknown as ReactKeyboardEvent<HTMLElement>,
      ),
    [],
  );
  const scrubPause = useRef<Readonly<{
    bridge: NonNullable<typeof timelineTransport.current>;
    task: Promise<void>;
  }> | null>(null);
  const queuedScrubSeek = useRef<Readonly<{
    bridge: NonNullable<typeof timelineTransport.current>;
    frame: number;
  }> | null>(null);
  const bindPause = useCallback((pause: (() => Promise<void>) | null) => {
    pauseMonitor.current = pause;
  }, []);
  const beginTrim = useCallback(() => {
    void pauseMonitor.current?.().catch(() => undefined);
  }, []);
  const bindTimelineTransport = useCallback(
    (
      next: Readonly<{
        seek(frame: number): void;
        pause(): Promise<void>;
        toggle(): void;
        step(direction: -1 | 1): void;
        available: boolean;
      }> | null,
    ) => {
      timelineTransport.current = next;
      if (scrubPause.current?.bridge !== next) scrubPause.current = null;
      if (queuedScrubSeek.current?.bridge !== next)
        queuedScrubSeek.current = null;
      playhead.publishAvailability(next?.available ?? false);
      // IMPORTANT: an obsolete monitor cleanup may run after its replacement has registered.
      // Clear only the bridge this registration owns or ruler input can disable the new session.
      return () => {
        if (timelineTransport.current === next) {
          timelineTransport.current = null;
          if (scrubPause.current?.bridge === next) scrubPause.current = null;
          if (queuedScrubSeek.current?.bridge === next)
            queuedScrubSeek.current = null;
        }
      };
    },
    [playhead],
  );
  const seekFromTimeline = useCallback(
    (frame: number) => {
      // The workspace position is authoring state, not a decoder acknowledgement. Publish it before
      // consulting the monitor so an unavailable preview still supports navigation and exact edits.
      playhead.publish(frame);
      const bridge = timelineTransport.current;
      if (bridge === null || !bridge.available) return;
      // IMPORTANT: section 4.8 requires the first seek to wait for one pause. Keep only the newest
      // target while that pause settles; firing pause and seek concurrently can revoke the media
      // acquisition and leave the accepted monitor blocked as source_unavailable.
      if (scrubPause.current?.bridge === bridge) {
        queuedScrubSeek.current = { bridge, frame };
        return;
      }
      bridge.seek(frame);
    },
    [playhead],
  );
  const publishPresentedFrame = useCallback(
    (frame: number | null) => {
      // GUARD: `null` means the preview has no presentation owner; it is not a workspace seek.
      // Clearing the logical position here makes an unavailable monitor reset accepted edits to
      // frame zero before the new content extent can clamp the user's retained position.
      if (frame !== null) playhead.publish(frame);
    },
    [playhead],
  );
  const pauseForScrub = useCallback(() => {
    const bridge = timelineTransport.current;
    if (
      bridge === null ||
      !bridge.available ||
      scrubPause.current?.bridge === bridge
    )
      return;
    const task = bridge.pause();
    const pending = { bridge, task } as const;
    scrubPause.current = pending;
    void task.then(
      () => {
        if (scrubPause.current !== pending) return;
        scrubPause.current = null;
        const queued = queuedScrubSeek.current;
        if (queued?.bridge !== bridge) return;
        queuedScrubSeek.current = null;
        if (timelineTransport.current === bridge) bridge.seek(queued.frame);
      },
      () => {
        if (scrubPause.current === pending) scrubPause.current = null;
        if (queuedScrubSeek.current?.bridge === bridge)
          queuedScrubSeek.current = null;
      },
    );
  }, []);
  // Keep per-frame transport updates out of manifest construction; only accepted
  // source or capability changes may rebuild the monitor's media binding.
  const monitor = useMemo(
    () => deriveNleMonitorBinding(monitorSnapshot, runtime, leaseClient),
    [monitorSnapshot, runtime, leaseClient],
  );
  // M25-21: the last presented frame is kept for the same composition workspace and restored
  // paused on the next monitor open; playback state itself is never retained.
  const transportScope =
    authoringV2?.workspaceHandle ?? monitorSnapshot?.workspaceHandle ?? null;
  const transportScopeRef = useRef(transportScope);
  transportScopeRef.current = transportScope;
  const retainedTransport = useRetainedSlot(
    binding.retention,
    "nle.transport",
    transportScope,
  );
  const retainedFrame = useMemo(
    () => ({
      restore: () => {
        const scope = transportScopeRef.current;
        return scope === null
          ? null
          : (retainedTransport.restoreLate(scope).value?.frame ?? null);
      },
      retain: (frame: number) =>
        retainedTransport.write(transportScopeRef.current, { frame }),
    }),
    [retainedTransport],
  );
  const logicalLastFrame = Math.max(
    0,
    (authoringV2?.contentEndExclusive ??
      (snapshot === undefined ? 0 : Number(snapshot.output.durationFrames))) -
      1,
  );
  useLayoutEffect(() => {
    const current = playhead.snapshot().frame ?? 0;
    playhead.publish(Math.min(logicalLastFrame, Math.max(0, current)));
  }, [logicalLastFrame, playhead, transportScope]);
  const selectedClip: CompositionClip | undefined = snapshot?.clips.find(
    (clip) => clip.clipId === selection[0],
  );
  const selectedTrack = snapshot?.tracks.find(
    (track) => track.trackId === selectedClip?.trackId,
  );
  const transformEditable =
    selection.length === 1 &&
    selectedClip?.enabled === true &&
    selectedTrack?.enabled === true &&
    selectedTrack.locked === false &&
    authoring.status === "ready";
  const transformAuthority =
    snapshot === undefined
      ? "absent"
      : `${snapshot.timelineRevision}:${snapshot.timelineFingerprint}:${
          authoring.status === "conflict" || authoring.status === "error"
            ? authoring.status
            : "accepted"
        }`;
  const commitMonitorTransform = useCallback(
    (transform: TransformWire) => {
      if (!transformEditable || selectedClip === undefined)
        return Promise.resolve();
      return timelineIntent({
        action: "apply_timeline_commands",
        commands: [build.setVisualTransform(selectedClip.clipId, transform)],
      });
    },
    [selectedClip, timelineIntent, transformEditable],
  );
  const pane = state.surface.pane;
  const highlighted = state.import.highlightedAssetIds;
  const productionProjection =
    "projection" in binding.production
      ? binding.production.projection
      : undefined;
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const tabKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let target: number | null = null;
    if (event.key === "ArrowRight") target = (index + 1) % BIN_TABS.length;
    else if (event.key === "ArrowLeft")
      target = (index - 1 + BIN_TABS.length) % BIN_TABS.length;
    else if (event.key === "Home") target = 0;
    else if (event.key === "End") target = BIN_TABS.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      actions.selectPane(BIN_TABS[index]![0]);
      return;
    }
    if (target === null) return;
    event.preventDefault();
    tabRefs.current[target]?.focus();
  };

  // The import highlight is a one-shot cue; clear it once the user acts on the timeline.
  useEffect(() => {
    if (highlighted.length === 0) return;
    const timer = setTimeout(() => actions.clearImportHighlight(), 8_000);
    return () => clearTimeout(timer);
  }, [highlighted, actions]);

  // M25-63 (R9): the save indicator's model. The top bar shows its label; the visually hidden
  // live region below announces its sentence and carries the revision as an attribute only.
  const save = saveIndicatorModel(authoring, locale);
  // M25-44 (one NLE): the retired compact editor was the only place to start a workspace. The
  // empty timeline offers the same action for every state that has no workspace to show.
  const canStart =
    authoring.status === "absent" ||
    authoring.status === "released" ||
    authoring.status === "gone" ||
    (authoring.status === "error" && projection === undefined);

  // M25-63: the editor's Import leads the bin toolbar; its status and Media tools card render in
  // the bin-foot slot below the cards (`ImportToEditor` variant "toolbar").
  const [importNotice, setImportNotice] = useState<HTMLDivElement | null>(null);
  const importControl =
    productionProjection !== undefined &&
    binding.importAction !== undefined &&
    productionProjection.allowedActions.includes(
      "import_production_outputs_to_authoring",
    ) ? (
      <ImportToEditor
        locale={locale}
        projection={productionProjection}
        busy={
          binding.production.status === "loading" ||
          binding.production.status === "pending"
        }
        binding={binding.importAction}
        variant="toolbar"
        noticeSlot={importNotice}
      />
    ) : null;

  const bin = (
    <div className="h3-nle-bin">
      <div
        className="h3-nle-tabs"
        role="tablist"
        aria-label={text.panes.switcher}
      >
        {BIN_TABS.map(([id, icon], index) => (
          <button
            key={id}
            ref={(element) => {
              tabRefs.current[index] = element;
            }}
            type="button"
            role="tab"
            data-h3-plain
            id={`h3-nle-tab-${id}`}
            aria-controls={`h3-nle-panel-${id}`}
            aria-selected={pane === id}
            tabIndex={pane === id ? 0 : -1}
            aria-label={text.panes[id]}
            title={text.panes.describe[id]}
            data-h3-nle-pane={id}
            onClick={() => actions.selectPane(id)}
            onKeyDown={(event) => tabKeyDown(event, index)}
          >
            <NleActionIcon name={icon} />
            <span className="h3-nle-tab-label">{text.panes[id]}</span>
          </button>
        ))}
      </div>
      <div
        id={`h3-nle-panel-${pane}`}
        className="h3-nle-area-scroll"
        role="tabpanel"
        aria-labelledby={`h3-nle-tab-${pane}`}
      >
        {pane === "sequence" ? (
          <div className="h3-nle-sequence-home">
            <NleSequencePanel binding={binding} />
          </div>
        ) : pane === "text" ? (
          authoringV2 !== undefined || snapshot !== undefined ? (
            <NleTextBin
              locale={locale}
              snapshot={authoringV2 ?? snapshot!}
              busy={
                authoring.status === "pending" || authoring.status === "loading"
              }
              currentFrame={() => playhead.snapshot().frame ?? 0}
              subscribePlayhead={playhead.subscribe}
              onIntent={timelineIntent}
            />
          ) : (
            <p className="h3-nle-note">{text.assets.empty}</p>
          )
        ) : (
          <div className="h3-nle-media-home">
            {authoringV2 !== undefined ? (
              <NleAuthoringAssetBin
                locale={locale}
                authoring={authoringV2}
                binInsert={binInsert}
                highlightedAssetIds={highlighted}
                runtimeEpoch={state.surface.generation}
                leaseClient={leaseClient}
                decorationCache={mediaDecorationCache}
                decorationDemandBroker={decorationDemandBroker}
                currentFrame={() => playhead.snapshot().frame ?? 0}
                busy={
                  authoring.status === "pending" ||
                  authoring.status === "loading"
                }
                lead={importControl}
                onIntent={timelineIntent}
              />
            ) : snapshot !== undefined ? (
              <NleMediaBin
                locale={locale}
                snapshot={snapshot}
                highlightedAssetIds={highlighted}
                busy={
                  authoring.status === "pending" ||
                  authoring.status === "loading"
                }
                runtimeEpoch={state.surface.generation}
                leaseClient={leaseClient}
                decorationCache={mediaDecorationCache}
                decorationDemandBroker={decorationDemandBroker}
                currentFrame={() => playhead.snapshot().frame ?? 0}
                binInsert={binInsert}
                authoringFingerprint={authoringFingerprint}
                lead={importControl}
                onIntent={timelineIntent}
              />
            ) : (
              <>
                {importControl === null ? null : (
                  <div className="h3-nle-media-controls">
                    <div className="h3-nle-media-toolbar">{importControl}</div>
                  </div>
                )}
                <p className="h3-nle-note">{text.assets.empty}</p>
              </>
            )}
            {/* M25-63: the bin-foot notice slot. The editor's Import renders its status and the
                Media tools card here, so they never push the toolbar row apart. */}
            <div className="h3-nle-bin-foot" ref={setImportNotice} />
          </div>
        )}
      </div>
    </div>
  );

  const timeline = (
    <div className="h3-nle-timeline-area">
      {snapshot !== undefined ? (
        <TransportTimeline
          locale={locale}
          snapshot={snapshot}
          selection={selection}
          authoring={authoring}
          gridFrames={projection?.timeline.profile.frameGrid ?? 1}
          editCapacityFrames={authoringV2?.editCapacityFrames}
          contentEndExclusive={authoringV2?.contentEndExclusive}
          channel={playhead}
          highlightedAssetIds={highlighted}
          onIntent={timelineIntent}
          rebaseAttempt={timelineAttempt.current}
          onEdgeGestureActive={onEdgeGestureActive}
          onBeginTrim={beginTrim}
          onSeek={seekFromTimeline}
          onBeginScrub={pauseForScrub}
          onTogglePlay={() => {
            const bridge = timelineTransport.current;
            if (bridge?.available) bridge.toggle();
          }}
          onStep={(direction) => {
            const current = playhead.snapshot().frame ?? 0;
            seekFromTimeline(
              Math.min(logicalLastFrame, Math.max(0, current + direction)),
            );
          }}
          bindShortcuts={bindTimelineShortcuts}
          retention={binding.retention}
          binInsert={binInsert}
          // IMPORTANT: populated V2 authoring still renders the accepted public snapshot. Keep
          // timeline decoration ownership connected or its clips silently lose filmstrip and
          // audio-peaks demand while the media bin continues to acquire thumbnails.
          decorationDemandBroker={decorationDemandBroker}
          leaseClient={leaseClient}
          runtimeEpoch={state.surface.generation}
        />
      ) : (
        <section
          className="h3-nle-timeline"
          aria-label={text.timeline.title}
          data-h3-nle-region="timeline"
          data-h3-nle-edit-capacity={authoringV2?.editCapacityFrames}
        >
          <p className="h3-nle-note" role="status">
            {save.sentence}
          </p>
          {authoringV2 !== undefined ? (
            <>
              {historyV2 === undefined ? null : (
                <EmptyV2TimelineHistory
                  locale={locale}
                  status={authoring.status}
                  history={historyV2}
                  onIntent={timelineIntent}
                />
              )}
              <p className="h3-nle-note">{text.timeline.empty}</p>
              <div
                className="h3-nle-empty-origin"
                data-h3-nle-empty-origin="0"
                aria-label={formatTimelineTimecode(0, 24)}
              />
              <p className="h3-nle-note">
                {fill(text.summary.editCapacity, {
                  timecode: formatTimelineTimecode(
                    authoringV2.editCapacityFrames,
                    24,
                  ),
                })}
              </p>
              <NleEmptyTimelineDrop
                locale={locale}
                authoring={authoringV2}
                channel={binInsert}
                busy={
                  authoring.status !== "ready" &&
                  authoring.status !== "conflict"
                }
                onIntent={timelineIntent}
              />
            </>
          ) : null}
          {canStart ? (
            <div className="h3-nle-actions">
              <button
                type="button"
                data-h3-nle-action="start-authoring"
                disabled={!binding.contextAvailable}
                title={
                  binding.contextAvailable
                    ? undefined
                    : text.summary.startUnavailable
                }
                onClick={() => void actions.startAuthoring()}
              >
                {text.summary.start}
              </button>
            </div>
          ) : null}
        </section>
      )}
      <p
        className="h3-nle-vh"
        role="status"
        aria-live="polite"
        data-h3-nle-status="timeline"
        data-h3-nle-authoring={authoring.status}
        data-h3-nle-rejection={historyRejection?.code}
        data-h3-transaction={receipt?.transactionId ?? receiptV2?.transactionId}
        data-h3-nle-save-state={save.state}
        data-h3-nle-timeline-revision={save.revision ?? undefined}
      >
        {save.live}
      </p>
    </div>
  );

  return (
    <NleShell
      locale={locale}
      layout={state.surface.layout}
      onLayout={actions.setLayout}
      bin={bin}
      monitor={
        <NleMonitor
          locale={locale}
          monitor={monitor}
          leaseScheduler={leaseScheduler}
          selectedClip={selectedClip}
          onFrame={publishPresentedFrame}
          onTimelineRequest={playhead.publishTimelineRequest}
          bindPause={bindPause}
          bindTimelineTransport={bindTimelineTransport}
          bindReplace={actions.bindMonitorReplace}
          retainedFrame={retainedFrame}
          logicalFrame={logicalFrame}
          sourceProjection={projection}
          openSourcePreview={binding.openSourcePreview}
          emptyTimeline={
            monitorSnapshot?.clips.length === 0 ||
            (historyV2 !== undefined && historyV2.authoring.clips.length === 0)
          }
          output={monitorSnapshot?.output}
          mediaTools={binding.mediaTools}
          overlayGeneration={state.surface.generation}
          statusChannel={monitorStatus}
          onPresentationMeasurement={onPresentationMeasurement}
          transformEditable={transformEditable}
          transformAuthority={transformAuthority}
          onTransformCommit={commitMonitorTransform}
        />
      }
      inspector={
        <div className="h3-nle-area-scroll h3-nle-inspector-scroll">
          {snapshot !== undefined ? (
            <NleInspectorTabs
              locale={locale}
              snapshot={snapshot}
              selection={selection}
              authoring={authoring}
              onIntent={timelineIntent}
              retention={binding.retention}
            />
          ) : (
            <p className="h3-nle-note">{text.inspector.noSelection}</p>
          )}
        </div>
      }
      timeline={timeline}
      onKeyDown={ownWorkspaceShortcut}
    />
  );
}
