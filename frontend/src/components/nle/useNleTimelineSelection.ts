import {
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
  type MouseEvent as ReactMouseEvent,
} from "react";
import type { CompositionClip } from "../../contracts/compositionCodec";
import {
  adjacentClipId,
  contiguousClipSelection,
} from "../../runtime/timelineNavigation";
import { clampViewStart } from "../../runtime/timelineGeometry";
import { build } from "./nleCommandBuilders";
import {
  timelineMenuLabels,
  type TimelineMenuTarget,
} from "./NleTimelineMenus";
import {
  NLE_ROW_HEIGHT_PX,
  clipEnd,
  type NleTimelineProps,
} from "./nleTimelineTypes";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";

type SelectionInput = Pick<
  NleTimelineProps,
  "locale" | "snapshot" | "selection" | "onIntent"
> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    | "tracks"
    | "scrollRef"
    | "setScrollTop"
    | "laneWidth"
    | "pixelsPerFrame"
    | "viewStart"
    | "setTimelineView"
    | "durationFrames"
    | "mountedClipIds"
  > &
  Readonly<{
    focusedClipId: string | null;
    setFocusedClipId: Dispatch<SetStateAction<string | null>>;
  }>;

/** Selection, menu focus and canonical selection intents share the facade's focus state. */
export function useNleTimelineSelection({
  locale,
  snapshot,
  selection,
  onIntent,
  tracks,
  scrollRef,
  setScrollTop,
  laneWidth,
  pixelsPerFrame,
  viewStart,
  setTimelineView,
  durationFrames,
  mountedClipIds,
  focusedClipId,
  setFocusedClipId,
}: SelectionInput) {
  const [clickSelectionMode, setClickSelectionMode] = useState<
    "replace" | "toggle" | "range"
  >("replace");
  const [trackMenuTarget, setTrackMenuTarget] =
    useState<TimelineMenuTarget | null>(null);
  const [clipMenuTarget, setClipMenuTarget] =
    useState<TimelineMenuTarget | null>(null);
  const selectionAnchor = useRef<string | null>(selection[0] ?? null);
  const pendingFocusClipId = useRef<string | null>(null);
  const menuLabels = timelineMenuLabels(locale);
  // a click on the trigger button that opened a menu closes it. The menu's outside-press
  // dismissal leaves that button alone (`useMenuDismiss`), so this click is the toggle; a
  // context-menu gesture always (re)opens at the pointer.
  const menuOpening = (
    event: ReactMouseEvent<HTMLElement>,
    id: string | null,
  ) => {
    const opener = event.currentTarget;
    const toggles = event.type === "click";
    const next: TimelineMenuTarget = {
      id,
      returnFocus: opener,
      x: event.clientX,
      y: event.clientY,
    };
    return (current: TimelineMenuTarget | null) =>
      toggles && current?.returnFocus === opener ? null : next;
  };
  const openTrackMenu = (
    event: ReactMouseEvent<HTMLElement>,
    trackId: string | null,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    setClipMenuTarget(null);
    setTrackMenuTarget(menuOpening(event, trackId));
  };
  const openClipMenu = (
    event: ReactMouseEvent<HTMLElement>,
    clipId: string,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    setTrackMenuTarget(null);
    setClipMenuTarget(menuOpening(event, clipId));
  };
  const openClipMenuFromKey = (element: HTMLElement, clipId: string) => {
    const rect = element.getBoundingClientRect();
    setTrackMenuTarget(null);
    setClipMenuTarget({
      id: clipId,
      returnFocus: element,
      x: rect.left,
      y: rect.bottom,
    });
  };

  useLayoutEffect(() => {
    const clipId = pendingFocusClipId.current;
    if (clipId === null || !mountedClipIds.has(clipId)) return;
    const target = scrollRef.current?.querySelector<HTMLElement>(
      `[data-h3-nle-clip="${CSS.escape(clipId)}"] .h3-nle-clip-body`,
    );
    if (target === undefined || target === null) return;
    target.focus();
    pendingFocusClipId.current = null;
  }, [mountedClipIds]);
  const selected = useMemo(() => new Set(selection), [selection]);
  const orderedNavigationClips = useMemo(
    () =>
      snapshot.clips
        .map((clip) => ({
          clipId: clip.clipId,
          startFrame: clip.startFrame,
          trackOrder:
            tracks.find((track) => track.trackId === clip.trackId)?.order ?? 0,
        }))
        .sort(
          (left, right) =>
            left.trackOrder - right.trackOrder ||
            left.startFrame - right.startFrame ||
            left.clipId.localeCompare(right.clipId),
        ),
    [snapshot.clips, tracks],
  );
  const select = (
    clip: CompositionClip,
    mode: "replace" | "toggle" | "range",
  ) => {
    const anchor = selectionAnchor.current ?? selection[0] ?? clip.clipId;
    const next =
      mode === "range"
        ? contiguousClipSelection(orderedNavigationClips, anchor, clip.clipId)
        : mode === "toggle"
          ? selected.has(clip.clipId)
            ? selection.filter((member) => member !== clip.clipId)
            : [...selection, clip.clipId]
          : [clip.clipId];
    if (
      next.length === selection.length &&
      next.every((member, index) => member === selection[index])
    ) {
      setFocusedClipId(clip.clipId);
      return;
    }
    // IMPORTANT: focus is part of the same selection activation. Queue it before the async
    // transaction changes the external authoring store so React batches the local focus and
    // pending projection; placing it afterwards creates a fifth overlay commit for selection.
    setFocusedClipId(clip.clipId);
    void onIntent({
      action: "apply_timeline_commands",
      commands: [build.selectClips(next)],
    });
    if (mode !== "range") selectionAnchor.current = clip.clipId;
  };

  const revealClip = (clip: CompositionClip) => {
    setFocusedClipId(clip.clipId);
    const trackIndex = tracks.findIndex(
      (track) => track.trackId === clip.trackId,
    );
    const element = scrollRef.current;
    if (trackIndex >= 0 && element !== null) {
      const rowTop = trackIndex * NLE_ROW_HEIGHT_PX;
      const rowBottom = rowTop + NLE_ROW_HEIGHT_PX;
      const nextScrollTop =
        rowTop < element.scrollTop
          ? rowTop
          : rowBottom > element.scrollTop + element.clientHeight
            ? rowBottom - element.clientHeight
            : element.scrollTop;
      if (nextScrollTop !== element.scrollTop) {
        element.scrollTop = nextScrollTop;
        setScrollTop(nextScrollTop);
      }
    }
    const frameWidth = Math.max(1, laneWidth / pixelsPerFrame);
    if (clip.startFrame < viewStart || clipEnd(clip) > viewStart + frameWidth) {
      setTimelineView((current) =>
        Object.freeze({
          ...current,
          fitLocked: false,
          viewStart: clampViewStart(
            clip.startFrame - frameWidth / 4,
            durationFrames,
            laneWidth,
            current.pixelsPerFrame,
          ),
        }),
      );
    }
  };
  const selectAdjacent = (direction: -1 | 1) => {
    const id = adjacentClipId(
      orderedNavigationClips,
      focusedClipId ?? selection[0] ?? null,
      direction,
    );
    const clip = snapshot.clips.find((member) => member.clipId === id);
    if (clip === undefined) return;
    pendingFocusClipId.current = clip.clipId;
    revealClip(clip);
    select(clip, clickSelectionMode);
  };
  const replaceSelection = (ids: readonly string[]) => {
    if (
      ids.length === selection.length &&
      ids.every((id, index) => id === selection[index])
    )
      return;
    void onIntent({
      action: "apply_timeline_commands",
      commands: [build.selectClips(ids)],
    });
  };

  return {
    clickSelectionMode,
    setClickSelectionMode,
    trackMenuTarget,
    setTrackMenuTarget,
    clipMenuTarget,
    setClipMenuTarget,
    pendingFocusClipId,
    menuLabels,
    openTrackMenu,
    openClipMenu,
    openClipMenuFromKey,
    selected,
    orderedNavigationClips,
    select,
    revealClip,
    selectAdjacent,
    replaceSelection,
  };
}
