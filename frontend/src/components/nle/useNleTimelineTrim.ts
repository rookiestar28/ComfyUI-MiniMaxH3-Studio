import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type { CompositionClip } from "../../contracts/compositionCodec";
import {
  TRIM_GESTURE_IDLE,
  reduceTrimGesture,
  snapTrimBoundary,
  trimCommandForDraft,
  type TrimEdge,
  type TrimGestureState,
} from "../../runtime/nleTrimGesture";
import { build } from "./nleCommandBuilders";
import {
  clipEnd,
  timelineAuthoringContext,
  type NleTimelineProps,
} from "./nleTimelineTypes";
import type { useNleTimelineCapture } from "./useNleTimelineCapture";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";

type TrimInput = Pick<
  NleTimelineProps,
  | "snapshot"
  | "selection"
  | "authoring"
  | "gridFrames"
  | "playheadFrame"
  | "onIntent"
  | "onTrimState"
  | "onBeginTrim"
> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    | "mappingKey"
    | "pixelsPerFrame"
    | "durationFrames"
    | "snapEnabled"
    | "rippleEnabled"
  > &
  ReturnType<typeof useNleTimelineCapture> &
  Readonly<{
    context: ReturnType<typeof timelineAuthoringContext>;
    draftable: boolean;
  }>;

export function useNleTimelineTrim({
  snapshot,
  selection,
  authoring,
  gridFrames,
  playheadFrame,
  onIntent,
  onTrimState,
  onBeginTrim,
  mappingKey,
  pixelsPerFrame,
  durationFrames,
  snapEnabled,
  rippleEnabled,
  captureRef,
  releaseCapture,
  context,
  draftable,
}: TrimInput) {
  const {
    timelineAuthority,
    authoringFingerprint,
    capturedAuthoringIdentity,
    receiptId,
    rejection,
  } = context;
  const [gesture, setGesture] = useState<TrimGestureState>(TRIM_GESTURE_IDLE);
  const gestureRef = useRef(gesture);
  gestureRef.current = gesture;
  const submittedRequest = useRef<string | null>(null);
  const previousReceipt = useRef<string | null>(null);
  // A trim draft may begin on the accepted snapshot in `ready` and in `conflict`: after a
  // refusal the projection already carries the refreshed backend history, so a fresh draft
  // captures the current CAS. Requiring `ready` here leaves the row's keyboard buttons enabled
  // with no effect and the pointer grips dead until some unrelated command succeeds, which is
  // exactly the silent no-op control the matrix recovery cases reject. `pending`, `loading`,
  // `error` and `gone` still refuse: their snapshot is not an accepted base.
  const canBegin = (phase: TrimGestureState["phase"]) =>
    phase === "idle" || phase === "accepted" || phase === "rejected";
  // IMPORTANT: an edge draft belongs to the captured accepted snapshot. A new
  // selection, lock, catalog or timeline must cancel it, never silently rebase the cut.
  useEffect(() => {
    // Do not enqueue an idle updater: it adds a commit alongside external-store
    // replacement even when the updater eventually returns the same idle value.
    if (
      gestureRef.current.phase !== "dragging" &&
      gestureRef.current.phase !== "keyboard_draft"
    )
      return;
    setGesture((current) => {
      if (current.phase !== "dragging" && current.phase !== "keyboard_draft")
        return current;
      const identity = current.draft.identity;
      const clip = snapshot.clips.find(
        (member) => member.clipId === identity.clipId,
      );
      const track = snapshot.tracks.find(
        (member) => member.trackId === clip?.trackId,
      );
      const valid =
        selection[0] === identity.clipId &&
        track?.locked === false &&
        identity.workspaceHandle === timelineAuthority.workspaceHandle &&
        draftable &&
        identity.workspaceRevision === timelineAuthority.workspaceRevision &&
        identity.timelineRevision === timelineAuthority.timelineRevision &&
        identity.timelineFingerprint ===
          timelineAuthority.timelineFingerprint &&
        identity.authoringFingerprint === authoringFingerprint;
      return valid ? current : TRIM_GESTURE_IDLE;
    });
  }, [
    snapshot,
    selection,
    draftable,
    timelineAuthority.workspaceHandle,
    timelineAuthority.workspaceRevision,
    timelineAuthority.timelineRevision,
    timelineAuthority.timelineFingerprint,
    authoringFingerprint,
  ]);

  useEffect(() => {
    if (gesture.phase !== "dragging") releaseCapture();
  }, [gesture.phase]);

  // Settle a submitted trim from the accepted authoring state, never from the local draft.
  // IMPORTANT ( B3-D56): the accepted settle is derived while rendering the state that
  // accepted it, not in an effect afterwards. React re-renders immediately and commits once, so
  // the accepted revision and the trim's `accepted` notice reach the user in the same commit; run
  // from an effect it is a commit of its own, and the frozen per-edit commit ceiling has no room
  // for it. This is the supported "adjust state when a prop changes" pattern, not a render-time
  // side effect: it only calls this component's own setters, is guarded by `settledReceipt` so it
  // cannot loop, and starts nothing asynchronous. The refusal, lost-response and reconciliation
  // branches stay in the effect below -- they are not on this path and must not be made to race
  // with it. `previousReceipt` still marks the receipt that was current when the trim was
  // submitted, so only a genuinely new receipt settles it.
  const [settledReceipt, setSettledReceipt] = useState<string | null>(null);
  if (
    (gesture.phase === "submitting" || gesture.phase === "reconciling") &&
    authoring.status === "ready" &&
    receiptId !== null &&
    receiptId !== previousReceipt.current &&
    receiptId !== settledReceipt
  ) {
    setSettledReceipt(receiptId);
    setGesture((current) => reduceTrimGesture(current, { type: "accepted" }));
  }
  useEffect(() => {
    if (gesture.phase !== "submitting" && gesture.phase !== "reconciling")
      return;
    if (authoring.status === "pending" || authoring.status === "loading")
      return;
    if (
      authoring.status === "ready" &&
      receiptId !== null &&
      receiptId !== previousReceipt.current
    )
      return;
    if (authoring.status === "conflict") {
      setGesture((current) =>
        reduceTrimGesture(current, {
          type: "rejected",
          reason: rejection ?? "conflict",
        }),
      );
      return;
    }
    if (authoring.status === "error" || authoring.status === "gone") {
      //  corrective F1: a lost reply is reported by the session as
      // `error/outcome_unknown`, never by a rejected intent promise. It must read as unknown
      // (the reducer's `response_lost` -> `reconciling`), not as a rejection, until the
      // session's single read-only reconciliation arrives as `ready`.
      const lost =
        authoring.status === "error" && authoring.reason === "outcome_unknown";
      setGesture((current) =>
        reduceTrimGesture(current, {
          type: lost ? "response_lost" : "rejected",
          reason: authoring.reason,
        }),
      );
      return;
    }
    if (gesture.phase === "reconciling" && authoring.status === "ready")
      setGesture((current) =>
        reduceTrimGesture(current, { type: "reconciled" }),
      );
  }, [authoring, gesture.phase, receiptId, rejection]);

  useEffect(() => {
    if (gesture.phase !== "accepted" && gesture.phase !== "rejected") return;
    const timer = setTimeout(
      () =>
        setGesture((current) => reduceTrimGesture(current, { type: "reset" })),
      1_500,
    );
    return () => clearTimeout(timer);
  }, [gesture.phase]);

  const submit = (next: TrimGestureState) => {
    if (
      next.phase !== "submitting" ||
      submittedRequest.current === next.requestId
    )
      return;
    submittedRequest.current = next.requestId;
    previousReceipt.current = receiptId;
    const command = trimCommandForDraft(next.draft);
    // IMPORTANT: the Ripple toggle cancels every uncommitted grip before changing this mode.
    // Do not remove that cancellation or one drag can begin as trim_clip and commit ripple_trim.
    const target = snapshot.clips.find(
      (clip) => clip.clipId === command.clip_id,
    );
    if (target === undefined) return;
    void onIntent({
      action: "apply_timeline_commands",
      capturedTimeline: {
        workspaceHandle: next.draft.identity.workspaceHandle,
        workspaceRevision: next.draft.identity.workspaceRevision,
        timelineRevision: next.draft.identity.timelineRevision,
        timelineFingerprint: next.draft.identity.timelineFingerprint,
        ...("authoringFingerprint" in next.draft.identity &&
        next.draft.identity.authoringFingerprint !== undefined
          ? { authoringFingerprint: next.draft.identity.authoringFingerprint }
          : {}),
      },
      commands: [
        rippleEnabled
          ? build.rippleTrim(
              command.clip_id,
              command.edge,
              command.delta_frames,
              [target.trackId],
            )
          : build.trimClip(command.clip_id, command.edge, command.delta_frames),
      ],
    });
  };

  const identityFor = (clip: CompositionClip, edge: TrimEdge) => ({
    clipId: clip.clipId,
    workspaceHandle: timelineAuthority.workspaceHandle,
    edge,
    workspaceRevision: timelineAuthority.workspaceRevision,
    timelineRevision: timelineAuthority.timelineRevision,
    timelineFingerprint: timelineAuthority.timelineFingerprint,
    ...capturedAuthoringIdentity,
    mappingKey,
  });

  const snapFor = (clip: CompositionClip) => (rawBoundaryFrame: number) =>
    snapEnabled
      ? snapTrimBoundary({
          rawBoundaryFrame,
          pixelsPerFrame,
          clips: snapshot.clips.map((member) => ({
            clipId: member.clipId,
            startFrame: member.startFrame,
            endFrame: clipEnd(member),
          })),
          targetClipId: clip.clipId,
          playheadFrame,
          outputDurationFrames: durationFrames,
        })
      : null;

  const beginPointer = (
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
    edge: TrimEdge,
  ) => {
    if (
      !canBegin(gestureRef.current.phase) ||
      event.button !== 0 ||
      event.isPrimary === false ||
      selection[0] !== clip.clipId ||
      !draftable ||
      snapshot.tracks.find((track) => track.trackId === clip.trackId)
        ?.locked !== false
    )
      return;
    event.currentTarget.setPointerCapture(event.pointerId);
    onBeginTrim?.();
    captureRef.current = {
      element: event.currentTarget,
      pointerId: event.pointerId,
    };
    const next = reduceTrimGesture(gestureRef.current, {
      type: "begin",
      input: "pointer",
      identity: identityFor(clip, edge),
      pointerId: event.pointerId,
      originClientX: event.clientX,
      pixelsPerFrame,
      originStart: clip.startFrame,
      originEnd: clipEnd(clip),
      outputDurationFrames: durationFrames,
    });
    // IMPORTANT: Chromium can deliver move/release before React commits pointerdown. Keep the
    // event ref synchronous or a fresh drag after conflict reads the rejected state and submits
    // nothing even though the visible grip moved.
    gestureRef.current = next;
    setGesture(next);
  };
  const movePointer = (
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ) => {
    const current = gestureRef.current;
    if (
      current.phase !== "dragging" ||
      current.draft.pointerId !== event.pointerId
    )
      return;
    const next = reduceTrimGesture(current, {
      type: "move",
      clientX: event.clientX,
      snap: snapFor(clip),
    });
    gestureRef.current = next;
    setGesture(next);
  };
  const releasePointer = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = gestureRef.current;
    if (
      current.phase !== "dragging" ||
      current.draft.pointerId !== event.pointerId
    )
      return;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    const next = reduceTrimGesture(current, {
      type: "release",
      requestId: `trim-${Date.now()}-${event.pointerId}`,
    });
    gestureRef.current = next;
    setGesture(next);
    submit(next);
  };
  const cancelPointer = (reason: string) => {
    // IMPORTANT ( B3-D56): only a live draft can be cancelled -- the reducer returns the
    // state untouched for every other phase. Enqueuing the updater anyway still schedules a render
    // React bails out of, and that bailed-out pass is a real commit against the per-edit ceiling.
    // It fires on the ordinary success path, not an edge case: `releasePointer` calls
    // `releasePointerCapture`, which synchronously raises `lostpointercapture` on a gesture that
    // has just become `submitting`, and the following focus move raises `blur` as well. Keep the
    // guard in step with the reducer's own `cancel` case rather than relying on the no-op.
    const current = gestureRef.current;
    if (current.phase !== "dragging" && current.phase !== "keyboard_draft")
      return;
    setGesture((state) => reduceTrimGesture(state, { type: "cancel", reason }));
  };

  const gripKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    clip: CompositionClip,
    edge: TrimEdge,
  ) => {
    const current = gestureRef.current;
    if (event.key === "Escape") {
      if (current.phase === "keyboard_draft" || current.phase === "dragging") {
        event.preventDefault();
        event.stopPropagation();
        cancelPointer("escape");
      }
      return;
    }
    if (event.key === "Enter") {
      if (current.phase !== "keyboard_draft") return;
      event.preventDefault();
      const next = reduceTrimGesture(current, {
        type: "commit",
        requestId: `trim-k-${Date.now()}`,
      });
      setGesture(next);
      submit(next);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    if (
      selection[0] !== clip.clipId ||
      !draftable ||
      snapshot.tracks.find((track) => track.trackId === clip.trackId)
        ?.locked !== false
    )
      return;
    event.preventDefault();
    const direction: 1 | -1 = event.key === "ArrowRight" ? 1 : -1;
    const steps = event.shiftKey ? Math.max(1, gridFrames) : 1;
    let next = current;
    if (canBegin(next.phase)) {
      onBeginTrim?.();
      next = reduceTrimGesture(next, {
        type: "begin",
        input: "keyboard",
        identity: identityFor(clip, edge),
        pointerId: null,
        originClientX: 0,
        pixelsPerFrame,
        originStart: clip.startFrame,
        originEnd: clipEnd(clip),
        outputDurationFrames: durationFrames,
      });
    }
    if (next.phase !== "keyboard_draft") return;
    for (let index = 0; index < steps; index += 1)
      next = reduceTrimGesture(next, { type: "step", deltaFrames: direction });
    gestureRef.current = next;
    setGesture(next);
  };

  return {
    gesture,
    gestureRef,
    setGesture,
    canBegin,
    submit,
    identityFor,
    snapFor,
    beginPointer,
    movePointer,
    releasePointer,
    cancelPointer,
    gripKeyDown,
  };
}
