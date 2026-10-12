import { useEffect, useRef } from "react";
import {
  decideTimelineTool,
  resolveTimelineShortcut,
  toolPlayheadFrame,
  type TimelineToolAction,
  type TimelineToolDecision,
} from "./nleTimelineTools";
import {
  timelineAuthoringContext,
  type NleTimelineProps,
  type NleTimelineShortcutHandler,
} from "./nleTimelineTypes";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type { useNleTimelineTransport } from "./useNleTimelineTransport";
import type { useNleTimelineSelection } from "./useNleTimelineSelection";
import type { useNleTimelineTrim } from "./useNleTimelineTrim";
import type { useNleTimelineDirectGesture } from "./useNleTimelineDirectGesture";
import type { useNleTimelineRoll } from "./useNleTimelineRoll";

type ToolsInput = Pick<
  NleTimelineProps,
  | "snapshot"
  | "selection"
  | "authoring"
  | "onIntent"
  | "rebaseAttempt"
  | "bindShortcuts"
  | "onTogglePlay"
  | "onStep"
> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    "rippleEnabled" | "zoomBy" | "fit"
  > &
  Pick<
    ReturnType<typeof useNleTimelineTransport>,
    | "rulerRequestedFrameRef"
    | "playheadFrameRef"
    | "navigationAvailableRef"
    | "transportAvailableRef"
    | "renderedToolKeyRef"
    | "currentToolKeyRef"
  > &
  Pick<
    ReturnType<typeof useNleTimelineSelection>,
    "orderedNavigationClips" | "replaceSelection"
  > &
  Pick<ReturnType<typeof useNleTimelineTrim>, "gestureRef" | "cancelPointer"> &
  Pick<
    ReturnType<typeof useNleTimelineDirectGesture>,
    "directGestureRef" | "cancelDirect"
  > &
  Pick<ReturnType<typeof useNleTimelineRoll>, "rollDraftRef"> &
  Readonly<{ context: ReturnType<typeof timelineAuthoringContext> }>;

export function useNleTimelineTools({
  snapshot,
  selection,
  authoring,
  onIntent,
  rebaseAttempt = null,
  bindShortcuts,
  onTogglePlay,
  onStep,
  context,
  rippleEnabled,
  zoomBy,
  fit,
  rulerRequestedFrameRef,
  playheadFrameRef,
  navigationAvailableRef,
  transportAvailableRef,
  renderedToolKeyRef,
  currentToolKeyRef,
  orderedNavigationClips,
  replaceSelection,
  gestureRef,
  cancelPointer,
  directGestureRef,
  cancelDirect,
  rollDraftRef,
}: ToolsInput) {
  const { timelineAuthority, capturedAuthoringIdentity, timelineHistoryV2 } =
    context;
  const shortcutHandlerRef = useRef<NleTimelineShortcutHandler>(
    () => undefined,
  );
  const timelineHistory =
    "timelineHistory" in authoring ? authoring.timelineHistory : undefined;
  const toolContext = () => ({
    snapshot,
    selection,
    authoringStatus: authoring.status,
    // CRITICAL: the latest requested ruler frame is the user's edit position even before the
    // monitor presents it. Falling back to the rendered state makes rapid seek-then-split edit
    // the previous frame and violates the request-generation transport authority.
    playheadFrame: toolPlayheadFrame({
      requestedFrame: rulerRequestedFrameRef.current,
      monitorFrame: playheadFrameRef.current,
      transportAvailable: navigationAvailableRef.current,
    }),
    rippleEnabled,
    undoCursor:
      timelineHistoryV2 !== undefined
        ? timelineHistoryV2.undoCursor
        : (timelineHistory?.undoCursor ?? null),
    redoCursor:
      timelineHistoryV2 !== undefined
        ? timelineHistoryV2.redoCursor
        : (timelineHistory?.redoCursor ?? null),
    rebaseAttempt,
  });
  const decide = (action: TimelineToolAction) =>
    decideTimelineTool(action, toolContext());
  const toolDecisions = {
    undo: decide({ kind: "undo" }),
    redo: decide({ kind: "redo" }),
    split: decide({ kind: "split" }),
    trim_start: decide({ kind: "trim_start" }),
    trim_end: decide({ kind: "trim_end" }),
    delete: decide({ kind: "delete" }),
    rebase: decide({ kind: "rebase" }),
  } as const;
  // B-M2564-05: the tools that read the playhead; the others change only with rendered inputs.
  const playheadToolKey = (
    split: TimelineToolDecision,
    trimStart: TimelineToolDecision,
    trimEnd: TimelineToolDecision,
  ) => `${split.enabled}:${trimStart.enabled}:${trimEnd.enabled}`;
  renderedToolKeyRef.current = playheadToolKey(
    toolDecisions.split,
    toolDecisions.trim_start,
    toolDecisions.trim_end,
  );
  currentToolKeyRef.current = () =>
    playheadToolKey(
      decide({ kind: "split" }),
      decide({ kind: "trim_start" }),
      decide({ kind: "trim_end" }),
    );
  const runTool = (action: TimelineToolAction) => {
    const decision = decide(action);
    if (!decision.enabled) return;
    cancelPointer("toolbar_action");
    cancelDirect("toolbar_action");
    void onIntent({
      action: "apply_timeline_commands",
      capturedTimeline: {
        workspaceHandle: timelineAuthority.workspaceHandle,
        workspaceRevision: timelineAuthority.workspaceRevision,
        timelineRevision: timelineAuthority.timelineRevision,
        timelineFingerprint: timelineAuthority.timelineFingerprint,
        ...capturedAuthoringIdentity,
      },
      commands: decision.commands,
    });
  };
  shortcutHandlerRef.current = (event) => {
    const resolved = resolveTimelineShortcut({
      key: event.key,
      ctrlKey: event.ctrlKey,
      metaKey: event.metaKey,
      shiftKey: event.shiftKey,
      altKey: event.altKey,
      repeat: event.repeat,
      isComposing: event.nativeEvent.isComposing,
      defaultPrevented: event.defaultPrevented,
      target: event.target,
    });
    if (resolved === null) return;
    event.preventDefault();
    event.stopPropagation();
    // CRITICAL: contain shortcuts before checking live draft refs. Keyboard move,
    // trim and roll own their commit/cancel lifecycle; selection or zoom would corrupt it.
    if (
      gestureRef.current.phase === "keyboard_draft" ||
      directGestureRef.current.phase === "keyboard_draft" ||
      rollDraftRef.current?.input === "keyboard"
    )
      return;
    if (!resolved.invoke) return;
    switch (resolved.action) {
      case "split":
      case "trim_start":
      case "trim_end":
      case "delete":
      case "undo":
      case "redo":
        runTool({ kind: resolved.action });
        return;
      case "ripple_delete":
        runTool({ kind: "delete", forceRipple: true });
        return;
      case "select_all":
        replaceSelection(
          orderedNavigationClips.slice(0, 128).map((clip) => clip.clipId),
        );
        return;
      case "zoom_in":
        zoomBy(2);
        return;
      case "zoom_out":
        zoomBy(0.5);
        return;
      case "zoom_fit":
        fit();
        return;
      case "toggle_play":
        if (transportAvailableRef.current) onTogglePlay?.();
        return;
      case "step_back":
        onStep?.(-1);
        return;
      case "step_forward":
        onStep?.(1);
    }
  };
  useEffect(() => {
    if (bindShortcuts === undefined) return;
    return bindShortcuts((event) => shortcutHandlerRef.current(event));
  }, [bindShortcuts]);

  return { decide, toolDecisions, runTool, shortcutHandlerRef };
}
