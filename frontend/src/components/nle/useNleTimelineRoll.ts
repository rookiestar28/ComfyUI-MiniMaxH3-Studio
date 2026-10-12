import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { quantizeSignedDelta } from "../../runtime/timelineGeometry";
import {
  eligibleRollCuts,
  type RollCut,
  type TimelineToolAction,
} from "./nleTimelineTools";
import type { NleTimelineProps } from "./nleTimelineTypes";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";

export function useNleTimelineRoll({
  snapshot,
  selection,
  gridFrames,
  onBeginTrim,
  pixelsPerFrameRef,
  draftable,
  isCompetingDraftActive,
  onRunTool,
}: Pick<
  NleTimelineProps,
  "snapshot" | "selection" | "gridFrames" | "onBeginTrim"
> &
  Pick<ReturnType<typeof useNleTimelineViewport>, "pixelsPerFrameRef"> &
  Readonly<{
    draftable: boolean;
    isCompetingDraftActive(): boolean;
    onRunTool(action: TimelineToolAction): void;
  }>) {
  const [rollDraft, setRollDraft] = useState<Readonly<{
    cut: RollCut;
    deltaFrames: number;
    input: "pointer" | "keyboard" | "click";
    pointerId: number | null;
    originClientX: number;
  }> | null>(null);
  const rollDraftRef = useRef(rollDraft);
  rollDraftRef.current = rollDraft;
  const suppressRollClickRef = useRef(false);
  useEffect(() => {
    const current = rollDraftRef.current;
    if (current === null) return;
    const stillOwned =
      draftable &&
      eligibleRollCuts(
        snapshot,
        snapshot.clips.find((clip) => clip.clipId === selection[0])?.trackId ??
          "",
      ).some(
        (cut) =>
          cut.leftClipId === current.cut.leftClipId &&
          cut.rightClipId === current.cut.rightClipId,
      );
    if (!stillOwned) setRollDraft(null);
  }, [snapshot, selection, draftable]);

  const selectedClip = snapshot.clips.find(
    (clip) => clip.clipId === selection[0],
  );
  const rollCuts = useMemo(() => {
    if (selectedClip === undefined) return Object.freeze([] as RollCut[]);
    const eligible = eligibleRollCuts(snapshot, selectedClip.trackId);
    // IMPORTANT: adjacent cuts can be closer than the 44 px target floor. Rendering both over
    // a narrow selected clip makes the later handle intercept the earlier one, so selection
    // owns one explicit cut: its right boundary first, then its left boundary at track end.
    const owned =
      eligible.find((cut) => cut.leftClipId === selectedClip.clipId) ??
      eligible.find((cut) => cut.rightClipId === selectedClip.clipId);
    return Object.freeze(owned === undefined ? [] : [owned]);
  }, [selectedClip?.clipId, selectedClip?.trackId, snapshot]);
  const cancelRoll = () => {
    if (rollDraftRef.current !== null) setRollDraft(null);
  };
  const beginRollPointer = (
    event: ReactPointerEvent<HTMLButtonElement>,
    cut: RollCut,
  ) => {
    if (
      !draftable ||
      event.button !== 0 ||
      event.isPrimary === false ||
      rollDraftRef.current !== null ||
      isCompetingDraftActive()
    )
      return;
    onBeginTrim?.();
    event.currentTarget.setPointerCapture(event.pointerId);
    setRollDraft({
      cut,
      deltaFrames: 0,
      input: "pointer",
      pointerId: event.pointerId,
      originClientX: event.clientX,
    });
  };
  const moveRollPointer = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = rollDraftRef.current;
    if (
      current === null ||
      current.input !== "pointer" ||
      current.pointerId !== event.pointerId
    )
      return;
    const delta =
      quantizeSignedDelta(
        (event.clientX - current.originClientX) / pixelsPerFrameRef.current,
      ) ?? 0;
    if (delta !== current.deltaFrames)
      setRollDraft({ ...current, deltaFrames: delta });
  };
  const commitRoll = (cut: RollCut, deltaFrames: number) => {
    if (deltaFrames !== 0)
      onRunTool({
        kind: "roll",
        leftClipId: cut.leftClipId,
        rightClipId: cut.rightClipId,
        deltaFrames,
      });
    setRollDraft(null);
  };
  const releaseRollPointer = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = rollDraftRef.current;
    if (
      current === null ||
      current.input !== "pointer" ||
      current.pointerId !== event.pointerId
    )
      return;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    suppressRollClickRef.current = current.deltaFrames !== 0;
    commitRoll(current.cut, current.deltaFrames);
  };
  const rollKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    cut: RollCut,
  ) => {
    const current = rollDraftRef.current;
    if (event.key === "Escape" && current?.cut === cut) {
      event.preventDefault();
      event.stopPropagation();
      cancelRoll();
      return;
    }
    if (event.key === "Enter" && current?.cut === cut) {
      event.preventDefault();
      event.stopPropagation();
      commitRoll(cut, current.deltaFrames);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    event.stopPropagation();
    const step =
      (event.shiftKey ? Math.max(1, gridFrames) : 1) *
      (event.key === "ArrowRight" ? 1 : -1);
    setRollDraft({
      cut,
      deltaFrames: current?.cut === cut ? current.deltaFrames + step : step,
      input: "keyboard",
      pointerId: null,
      originClientX: 0,
    });
  };
  //  (R7): the rail shows where the inline grips cannot -- below 24 px under a fine
  // pointer, below 144 px under a coarse one.
  // IMPORTANT: from the committed width, not the live draft that narrows the inline grips. While
  // a pointer holds a grip the rail's controls cannot be used, and a rail tied to the draft would
  // mount and unmount a layout row each time the drag crossed the threshold. It follows the
  // commit ( self-review).
  return {
    rollDraft,
    setRollDraft,
    rollDraftRef,
    suppressRollClickRef,
    selectedClip,
    rollCuts,
    cancelRoll,
    beginRollPointer,
    moveRollPointer,
    commitRoll,
    releaseRollPointer,
    rollKeyDown,
  };
}
