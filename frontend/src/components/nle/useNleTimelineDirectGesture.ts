import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type { CompositionClip } from "../../contracts/compositionCodec";
import {
  NLE_GESTURE_IDLE,
  admitTimelineInsert,
  admitTimelineMove,
  commandForGesture,
  draftOwnerPresent,
  reduceNleTimelineGesture,
  selectTimelineSnap,
  type NleTimelineGestureState,
  type TimelineSnapTarget,
} from "../../runtime/nleTimelineGesture";
import {
  LANE_LEAD_IN_PX,
  clampViewStart,
  contentXFromClientX,
  frameFromX,
  quantizeSignedDelta,
} from "../../runtime/timelineGeometry";
import { adjacentClipId } from "../../runtime/timelineNavigation";
import { build, freshIdentifier } from "./nleCommandBuilders";
import {
  DRAG_THRESHOLD_PX,
  NLE_ROW_HEIGHT_PX,
  clipEnd,
  timelineAuthoringContext,
  type NleTimelineProps,
} from "./nleTimelineTypes";
import type { useNleTimelineCapture } from "./useNleTimelineCapture";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import type { useNleTimelineTransport } from "./useNleTimelineTransport";
import type { useNleTimelineSelection } from "./useNleTimelineSelection";

type DirectInput = Pick<
  NleTimelineProps,
  | "snapshot"
  | "selection"
  | "authoring"
  | "gridFrames"
  | "onIntent"
  | "onBeginTrim"
  | "binInsert"
> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    | "tracks"
    | "mappingKey"
    | "viewStartRef"
    | "pixelsPerFrameRef"
    | "headerWidthRef"
    | "laneWidthRef"
    | "scrollRef"
    | "viewStart"
    | "pixelsPerFrame"
    | "laneWidth"
    | "durationFramesRef"
    | "durationFrames"
    | "setTimelineView"
    | "setScrollTop"
    | "snapEnabled"
    | "mountedClipIds"
  > &
  Pick<
    ReturnType<typeof useNleTimelineSelection>,
    | "selected"
    | "select"
    | "revealClip"
    | "orderedNavigationClips"
    | "pendingFocusClipId"
  > &
  Pick<ReturnType<typeof useNleTimelineTransport>, "playheadFrameRef"> &
  ReturnType<typeof useNleTimelineCapture> &
  Readonly<{
    context: ReturnType<typeof timelineAuthoringContext>;
    draftable: boolean;
  }>;

export function useNleTimelineDirectGesture({
  snapshot,
  selection,
  authoring,
  gridFrames,
  onIntent,
  onBeginTrim,
  binInsert,
  tracks,
  mappingKey,
  viewStartRef,
  pixelsPerFrameRef,
  headerWidthRef,
  laneWidthRef,
  scrollRef,
  viewStart,
  pixelsPerFrame,
  laneWidth,
  durationFramesRef,
  durationFrames,
  setTimelineView,
  setScrollTop,
  snapEnabled,
  mountedClipIds,
  selected,
  select,
  revealClip,
  orderedNavigationClips,
  pendingFocusClipId,
  playheadFrameRef,
  captureRef,
  releaseCapture,
  context,
  draftable,
}: DirectInput) {
  const {
    timelineAuthority,
    authoringFingerprint,
    capturedAuthoringIdentity,
    receiptId,
    rejection,
  } = context;
  const [directGesture, setDirectGesture] =
    useState<NleTimelineGestureState>(NLE_GESTURE_IDLE);
  const directGestureRef = useRef(directGesture);
  const publishedDirectGesture = useRef(directGesture);
  // IMPORTANT: pointerdown deliberately keeps its subthreshold draft outside React state.
  // An unrelated render must not erase it; authority/mapping cancellation reduces this ref.
  if (publishedDirectGesture.current !== directGesture) {
    publishedDirectGesture.current = directGesture;
    directGestureRef.current = directGesture;
  }
  const [clickMove, setClickMove] = useState<Readonly<{
    clipId: string;
    deltaFrames: number;
    rowDelta: number;
  }> | null>(null);
  const autoScrollMappingRef = useRef(false);
  const autoScrollRef = useRef<{
    raf: number | null;
    pointerX: number;
    pointerY: number;
    lastTime: number;
  }>({ raf: null, pointerX: 0, pointerY: 0, lastTime: 0 });
  const submittedDirectRequest = useRef<string | null>(null);
  const directPreviousReceipt = useRef<string | null>(null);
  const suppressClickRef = useRef(false);
  useEffect(() => {
    if (
      directGestureRef.current.phase !== "dragging" &&
      directGestureRef.current.phase !== "keyboard_draft"
    )
      return;
    const current = directGestureRef.current;
    const identity = current.draft.identity;
    const insertAssetId = current.draft.insert?.assetId ?? null;
    const valid =
      draftable &&
      identity.workspaceHandle === timelineAuthority.workspaceHandle &&
      identity.workspaceRevision === timelineAuthority.workspaceRevision &&
      identity.timelineRevision === timelineAuthority.timelineRevision &&
      identity.timelineFingerprint === timelineAuthority.timelineFingerprint &&
      identity.authoringFingerprint === authoringFingerprint &&
      (insertAssetId === null ||
        snapshot.assets.some((asset) => asset.assetId === insertAssetId)) &&
      identity.clipIds.every((clipId) =>
        snapshot.clips.some((clip) => clip.clipId === clipId),
      );
    if (!valid) cancelDirect("authority_changed");
  }, [
    draftable,
    snapshot,
    timelineAuthority.workspaceHandle,
    timelineAuthority.workspaceRevision,
    timelineAuthority.timelineRevision,
    timelineAuthority.timelineFingerprint,
    authoringFingerprint,
  ]);

  const [settledDirectReceipt, setSettledDirectReceipt] = useState<
    string | null
  >(null);
  // CRITICAL: settle a successful direct edit while rendering its accepted authoring state.
  // Moving this back into the effect below adds a fifth overlay commit to every move drag and
  // breaks the frozen <=4 per-edit ceiling; refusal and reconciliation still belong to the effect.
  if (
    (directGesture.phase === "submitting" ||
      directGesture.phase === "reconciling") &&
    authoring.status === "ready" &&
    receiptId !== null &&
    receiptId !== directPreviousReceipt.current &&
    receiptId !== settledDirectReceipt
  ) {
    setSettledDirectReceipt(receiptId);
    setDirectGesture((current) =>
      reduceNleTimelineGesture(current, { type: "accepted" }),
    );
  }
  useEffect(() => {
    if (
      directGesture.phase !== "submitting" &&
      directGesture.phase !== "reconciling"
    )
      return;
    if (authoring.status === "pending" || authoring.status === "loading")
      return;
    if (
      authoring.status === "ready" &&
      receiptId !== null &&
      receiptId !== directPreviousReceipt.current
    ) {
      setDirectGesture((current) =>
        reduceNleTimelineGesture(current, { type: "accepted" }),
      );
      return;
    }
    if (authoring.status === "conflict") {
      setDirectGesture((current) =>
        reduceNleTimelineGesture(current, {
          type: "rejected",
          reason: rejection ?? "conflict",
        }),
      );
      return;
    }
    if (authoring.status === "error" || authoring.status === "gone") {
      const lost =
        authoring.status === "error" && authoring.reason === "outcome_unknown";
      setDirectGesture((current) =>
        reduceNleTimelineGesture(current, {
          type: lost ? "response_lost" : "rejected",
          reason: authoring.reason,
        }),
      );
      return;
    }
    if (directGesture.phase === "reconciling" && authoring.status === "ready")
      setDirectGesture((current) =>
        reduceNleTimelineGesture(current, { type: "reconciled" }),
      );
  }, [authoring, directGesture.phase, receiptId, rejection]);

  useEffect(() => {
    if (
      directGesture.phase !== "accepted" &&
      directGesture.phase !== "rejected"
    )
      return;
    const timer = setTimeout(
      () =>
        setDirectGesture((current) =>
          reduceNleTimelineGesture(current, { type: "reset" }),
        ),
      1_500,
    );
    return () => clearTimeout(timer);
  }, [directGesture.phase]);

  const directOwnerMounted = draftOwnerPresent(
    directGestureRef.current,
    mountedClipIds,
  );
  useEffect(() => {
    if (directOwnerMounted) return;
    cancelDirect("owner_removed");
    if (document.activeElement === null || !document.activeElement.isConnected)
      scrollRef.current?.focus();
  }, [directOwnerMounted]);

  const gestureIdentity = (clipIds: readonly string[]) =>
    Object.freeze({
      workspaceHandle: timelineAuthority.workspaceHandle,
      workspaceRevision: timelineAuthority.workspaceRevision,
      timelineRevision: timelineAuthority.timelineRevision,
      timelineFingerprint: timelineAuthority.timelineFingerprint,
      ...capturedAuthoringIdentity,
      mappingKey,
      clipIds: Object.freeze([...clipIds]),
    });

  const targetTracksFor = (
    origins: readonly CompositionClip[],
    rowDelta: number,
  ) =>
    origins.map((clip) => {
      const index = tracks.findIndex((track) => track.trackId === clip.trackId);
      return tracks[index + rowDelta]?.trackId ?? "";
    });
  const admitMove = (
    origins: readonly CompositionClip[],
    targetTrackIds: readonly string[],
    deltaFrames: number,
  ) =>
    admitTimelineMove({
      origins,
      targetTrackIds,
      deltaFrames,
      durationFrames,
      tracks,
      clips: snapshot.clips,
      assets: snapshot.assets,
    });
  const snapMove = (
    origins: readonly CompositionClip[],
    deltaFrames: number,
    suspended: boolean,
  ) => {
    if (!snapEnabled || suspended || origins.length === 0) return null;
    const moving = new Set(origins.map((clip) => clip.clipId));
    const rawStart =
      Math.min(...origins.map((clip) => clip.startFrame)) + deltaFrames;
    const rawEnd = Math.max(...origins.map(clipEnd)) + deltaFrames;
    const candidates: TimelineSnapTarget[] = snapshot.clips
      .filter((clip) => !moving.has(clip.clipId))
      .flatMap((clip) => [
        { frame: clip.startFrame, kind: "clip_start" as const },
        { frame: clipEnd(clip), kind: "clip_end" as const },
      ]);
    if (playheadFrameRef.current !== null)
      candidates.push({ frame: playheadFrameRef.current, kind: "playhead" });
    const grid = Math.max(1, gridFrames);
    const gridBase = Math.round(rawStart / grid) * grid;
    candidates.push(
      { frame: gridBase, kind: "grid" },
      { frame: Math.round(rawEnd / grid) * grid, kind: "grid" },
    );
    const winner = selectTimelineSnap(
      rawStart,
      rawEnd,
      candidates,
      pixelsPerFrame,
    );
    return winner === null
      ? null
      : {
          deltaFrames: deltaFrames + winner.deltaFrames,
          lineFrame: winner.lineFrame,
        };
  };

  const stopAutoScroll = () => {
    const controller = autoScrollRef.current;
    if (controller.raf !== null) cancelAnimationFrame(controller.raf);
    controller.raf = null;
    controller.lastTime = 0;
  };
  const autoScrollVelocity = (
    coordinate: number,
    low: number,
    high: number,
  ) => {
    const edge = 32;
    if (coordinate < low + edge)
      return -480 * Math.min(1, Math.max(0, (low + edge - coordinate) / edge));
    if (coordinate > high - edge)
      return (
        480 * Math.min(1, Math.max(0, (coordinate - (high - edge)) / edge))
      );
    return 0;
  };
  const autoScrollTick = (time: number) => {
    const controller = autoScrollRef.current;
    controller.raf = null;
    const current = directGestureRef.current;
    const element = scrollRef.current;
    if (current.phase !== "dragging" || element === null || document.hidden) {
      stopAutoScroll();
      return;
    }
    const rect = element.getBoundingClientRect();
    const horizontalVelocity = autoScrollVelocity(
      controller.pointerX,
      rect.left + headerWidthRef.current,
      rect.right,
    );
    const verticalVelocity = autoScrollVelocity(
      controller.pointerY,
      rect.top,
      rect.bottom,
    );
    const elapsed = Math.min(
      50,
      Math.max(0, time - (controller.lastTime || time)),
    );
    controller.lastTime = time;
    const scale = pixelsPerFrameRef.current;
    const previousView = viewStartRef.current;
    const nextView = clampViewStart(
      previousView + (horizontalVelocity * elapsed) / 1_000 / scale,
      durationFrames,
      laneWidth,
      scale,
    );
    const previousScroll = element.scrollTop;
    if (verticalVelocity !== 0)
      element.scrollTop = Math.max(
        0,
        Math.min(
          element.scrollHeight - element.clientHeight,
          previousScroll + (verticalVelocity * elapsed) / 1_000,
        ),
      );
    const scrollDelta = element.scrollTop - previousScroll;
    if (nextView !== previousView || scrollDelta !== 0) {
      if (
        current.draft.kind === "insert_from_bin" &&
        current.draft.insert !== null
      ) {
        if (nextView !== previousView) {
          autoScrollMappingRef.current = true;
          viewStartRef.current = nextView;
          setTimelineView((current) =>
            Object.freeze({
              ...current,
              fitLocked: false,
              viewStart: nextView,
            }),
          );
        }
        const next = updateBinInsertDraft(
          current,
          controller.pointerX,
          controller.pointerY,
          nextView,
        );
        directGestureRef.current = next;
        setDirectGesture(next);
        if (horizontalVelocity !== 0 || verticalVelocity !== 0)
          controller.raf = requestAnimationFrame(autoScrollTick);
        return;
      }
      const desiredDelta =
        current.draft.deltaFrames + (nextView - previousView);
      let next = reduceNleTimelineGesture(current, {
        type: "auto_scroll_reanchor",
        mappingKey: `${scale}:${nextView}:${laneWidth}`,
        originClientX: controller.pointerX - desiredDelta * scale,
        originClientY: current.draft.originClientY - scrollDelta,
        originFrame: current.draft.originFrame,
      });
      const origins = current.draft.origins
        .map((origin) =>
          snapshot.clips.find((clip) => clip.clipId === origin.clipId),
        )
        .filter((clip): clip is CompositionClip => clip !== undefined);
      const rowDelta = Math.round(
        (controller.pointerY -
          (next.phase === "dragging"
            ? next.draft.originClientY
            : current.draft.originClientY)) /
          NLE_ROW_HEIGHT_PX,
      );
      const targets = targetTracksFor(origins, rowDelta);
      const admission = admitMove(origins, targets, desiredDelta);
      next = reduceNleTimelineGesture(next, {
        type: "move",
        clientX: controller.pointerX,
        clientY: controller.pointerY,
        targetTrackIds: targets,
        snap: null,
        ...admission,
      });
      directGestureRef.current = next;
      setDirectGesture(next);
      if (nextView !== previousView) {
        autoScrollMappingRef.current = true;
        viewStartRef.current = nextView;
        setTimelineView((current) =>
          Object.freeze({
            ...current,
            fitLocked: false,
            viewStart: nextView,
          }),
        );
      }
    }
    if (horizontalVelocity !== 0 || verticalVelocity !== 0)
      controller.raf = requestAnimationFrame(autoScrollTick);
  };
  const updateAutoScroll = (clientX: number, clientY: number) => {
    const controller = autoScrollRef.current;
    controller.pointerX = clientX;
    controller.pointerY = clientY;
    const element = scrollRef.current;
    if (element === null) return;
    const rect = element.getBoundingClientRect();
    const active =
      autoScrollVelocity(
        clientX,
        rect.left + headerWidthRef.current,
        rect.right,
      ) !== 0 || autoScrollVelocity(clientY, rect.top, rect.bottom) !== 0;
    if (!active) {
      stopAutoScroll();
      return;
    }
    if (controller.raf === null) {
      controller.lastTime = 0;
      controller.raf = requestAnimationFrame(autoScrollTick);
    }
  };

  const updateBinInsertDraft = (
    current: NleTimelineGestureState,
    clientX: number,
    clientY: number,
    mappedViewStart = viewStartRef.current,
  ): NleTimelineGestureState => {
    if (
      current.phase !== "dragging" ||
      current.draft.kind !== "insert_from_bin" ||
      current.draft.insert === null
    )
      return current;
    const element = scrollRef.current;
    const insert = current.draft.insert;
    const asset = snapshot.assets.find(
      (candidate) => candidate.assetId === insert.assetId,
    );
    if (element === null || asset === undefined)
      return reduceNleTimelineGesture(current, {
        type: "update_insert_from_bin",
        clientX,
        clientY,
        targetFrame: 0,
        targetTrackId: null,
        durationFrames: insert.durationFrames,
        admitted: false,
        reason: "target_exhaustion",
      });
    const rect = element.getBoundingClientRect();
    const inside =
      clientX >= rect.left + headerWidthRef.current + LANE_LEAD_IN_PX &&
      clientX <= rect.right &&
      clientY >= rect.top &&
      clientY <= rect.bottom;
    const row = Math.floor(
      (clientY - rect.top + element.scrollTop) / NLE_ROW_HEIGHT_PX,
    );
    const targetTrack = inside ? tracks[row] : undefined;
    const targetFrame = frameFromX(
      contentXFromClientX(clientX, rect.left, headerWidthRef.current),
      mappedViewStart,
      pixelsPerFrameRef.current,
      durationFrames,
    );
    const clippedDuration = Math.min(
      insert.sourceDurationFrames,
      durationFrames - targetFrame,
    );
    const admission =
      targetTrack === undefined || timelineAuthority.clips.length >= 128
        ? {
            admitted: false,
            reason: "target_exhaustion" as const,
            trackId: null,
          }
        : admitTimelineInsert({
            asset,
            startFrame: targetFrame,
            durationFrames: clippedDuration,
            timelineDurationFrames: durationFrames,
            tracks,
            clips: snapshot.clips,
            targetTrackId: targetTrack.trackId,
          });
    return reduceNleTimelineGesture(current, {
      type: "update_insert_from_bin",
      clientX,
      clientY,
      targetFrame,
      targetTrackId: admission.trackId,
      durationFrames: clippedDuration,
      admitted: admission.admitted,
      reason: admission.reason,
    });
  };

  const beginMove = (
    event: ReactPointerEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ) => {
    if (!draftable || event.button !== 0 || event.isPrimary === false) return;
    const movingIds = selected.has(clip.clipId) ? selection : [clip.clipId];
    const origins = snapshot.clips.filter((member) =>
      movingIds.includes(member.clipId),
    );
    if (origins.length === 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    captureRef.current = {
      element: event.currentTarget,
      pointerId: event.pointerId,
    };
    onBeginTrim?.();
    const next = reduceNleTimelineGesture(directGestureRef.current, {
      type: "begin_move",
      input: "pointer",
      identity: gestureIdentity(movingIds),
      pointerId: event.pointerId,
      originClientX: event.clientX,
      originClientY: event.clientY,
      pixelsPerFrame,
      origins: origins.map((member) => ({
        clipId: member.clipId,
        trackId: member.trackId,
        startFrame: member.startFrame,
      })),
      trackIds: tracks.map((track) => track.trackId),
      targetTrackIds: origins.map((member) => member.trackId),
    });
    // IMPORTANT: pointerdown is still a possible selection click. Keep its captured draft in the
    // ref until movement reaches the 4 px drag threshold; rendering it immediately makes every
    // click pay a begin/end gesture pair before its selection transaction and breaks the <=4
    // accepted-edit commit ceiling. The first qualifying pointermove publishes the same draft.
    directGestureRef.current = next;
  };
  const moveDirect = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = directGestureRef.current;
    if (
      current.phase !== "dragging" ||
      current.draft.pointerId !== event.pointerId
    )
      return;
    const origins = current.draft.origins
      .map((origin) =>
        snapshot.clips.find((clip) => clip.clipId === origin.clipId),
      )
      .filter((clip): clip is CompositionClip => clip !== undefined);
    const rowDelta = Math.round(
      (event.clientY - current.draft.originClientY) / NLE_ROW_HEIGHT_PX,
    );
    const targets = targetTracksFor(origins, rowDelta);
    const rawDelta =
      quantizeSignedDelta(
        (event.clientX - current.draft.originClientX) / pixelsPerFrame,
      ) ?? 0;
    const snapped = snapMove(origins, rawDelta, event.altKey);
    const delta = snapped?.deltaFrames ?? rawDelta;
    const admission = admitMove(origins, targets, delta);
    const next = reduceNleTimelineGesture(current, {
      type: "move",
      clientX: event.clientX,
      clientY: event.clientY,
      targetTrackIds: targets,
      snap:
        snapped === null
          ? null
          : current.draft.origins[0]!.startFrame + snapped.deltaFrames,
      snapLine: snapped?.lineFrame ?? null,
      ...admission,
    });
    directGestureRef.current = next;
    if (next.phase !== "dragging" || next.draft.movementPx < DRAG_THRESHOLD_PX)
      return;
    updateAutoScroll(event.clientX, event.clientY);
    setDirectGesture(next);
  };
  const submitDirect = (next: NleTimelineGestureState) => {
    if (
      next.phase !== "submitting" ||
      submittedDirectRequest.current === next.requestId
    )
      return;
    const command = commandForGesture(next);
    if (command === null) return;
    submittedDirectRequest.current = next.requestId;
    directPreviousReceipt.current = receiptId;
    const commands = [];
    const captured = next.draft.identity.clipIds;
    if (captured.some((id) => !selection.includes(id)))
      commands.push(build.selectClips(captured));
    if (command.kind === "move_clip")
      commands.push(
        build.moveClip(
          command.clipId,
          command.deltaFrames,
          command.targetTrackId,
        ),
      );
    else if (command.kind === "move_group")
      commands.push(
        build.moveGroup(
          command.clipIds.map(
            (clipId, index) =>
              [clipId, command.targetTrackIds[index]!] as const,
          ),
          command.deltaFrames,
        ),
      );
    else
      commands.push(
        build.insertAssetClip({
          clipId: freshIdentifier(timelineAuthority, "clip"),
          trackId: command.targetTrackId,
          assetId: command.assetId,
          startFrame: command.startFrame,
          durationFrames: command.durationFrames,
          sourceStartFrame: 0,
          text: null,
        }),
      );
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
      commands,
    });
  };
  useEffect(() => {
    if (binInsert === undefined) return;
    let ownedPointer: number | null = null;
    const unsubscribe = binInsert.subscribe((event) => {
      if (event.type === "begin") {
        const authority = event.authority;
        if (
          !draftable ||
          authority.workspaceHandle !== timelineAuthority.workspaceHandle ||
          authority.workspaceRevision !== timelineAuthority.workspaceRevision ||
          authority.timelineRevision !== timelineAuthority.timelineRevision ||
          authority.timelineFingerprint !==
            timelineAuthority.timelineFingerprint ||
          authority.authoringFingerprint !== authoringFingerprint ||
          !snapshot.assets.some((asset) => asset.assetId === authority.assetId)
        ) {
          binInsert.cancel("authority_unavailable", event.pointerId);
          return;
        }
        const next = reduceNleTimelineGesture(directGestureRef.current, {
          type: "begin_insert_from_bin",
          identity: {
            workspaceHandle: authority.workspaceHandle,
            workspaceRevision: authority.workspaceRevision,
            timelineRevision: authority.timelineRevision,
            timelineFingerprint: authority.timelineFingerprint,
            ...(authority.authoringFingerprint === undefined
              ? {}
              : { authoringFingerprint: authority.authoringFingerprint }),
            mappingKey,
            clipIds: [],
          },
          pointerId: event.pointerId,
          originClientX: event.clientX,
          originClientY: event.clientY,
          pixelsPerFrame: pixelsPerFrameRef.current,
          assetId: authority.assetId,
          durationFrames: authority.durationFrames,
        });
        if (next === directGestureRef.current || next.phase !== "dragging") {
          binInsert.cancel("gesture_busy", event.pointerId);
          return;
        }
        ownedPointer = event.pointerId;
        directGestureRef.current = next;
        onBeginTrim?.();
        return;
      }
      const current = directGestureRef.current;
      if (
        current.phase !== "dragging" ||
        current.draft.kind !== "insert_from_bin" ||
        current.draft.pointerId !== event.pointerId
      )
        return;
      if (event.type === "cancel") {
        ownedPointer = null;
        stopAutoScroll();
        const next = reduceNleTimelineGesture(current, {
          type: "cancel",
          reason: event.reason,
        });
        directGestureRef.current = next;
        setDirectGesture(next);
        return;
      }
      const updated = updateBinInsertDraft(
        current,
        event.clientX,
        event.clientY,
      );
      if (event.type === "move") {
        directGestureRef.current = updated;
        if (
          updated.phase !== "dragging" ||
          updated.draft.movementPx < DRAG_THRESHOLD_PX
        )
          return;
        updateAutoScroll(event.clientX, event.clientY);
        setDirectGesture(updated);
        return;
      }
      stopAutoScroll();
      ownedPointer = null;
      const next = reduceNleTimelineGesture(updated, {
        type: "release",
        requestId: `insert-${Date.now()}-${event.pointerId}`,
      });
      directGestureRef.current = next;
      if (next.phase === "idle") {
        setDirectGesture(next);
        return;
      }
      setDirectGesture(next);
      submitDirect(next);
    });
    return () => {
      if (ownedPointer !== null)
        binInsert.cancel("receiver_rebound_or_unmounted", ownedPointer);
      unsubscribe();
    };
  }, [
    binInsert,
    draftable,
    mappingKey,
    snapshot,
    tracks,
    durationFrames,
    onBeginTrim,
    timelineAuthority.workspaceHandle,
    timelineAuthority.workspaceRevision,
    timelineAuthority.timelineRevision,
    timelineAuthority.timelineFingerprint,
    authoringFingerprint,
  ]);
  const releaseDirect = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = directGestureRef.current;
    if (
      current.phase !== "dragging" ||
      current.draft.pointerId !== event.pointerId
    )
      return;
    const next = reduceNleTimelineGesture(current, {
      type: "release",
      requestId: `move-${Date.now()}-${event.pointerId}`,
    });
    stopAutoScroll();
    suppressClickRef.current = current.draft.movementPx >= DRAG_THRESHOLD_PX;
    directGestureRef.current = next;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    // A sub-threshold pointer sequence was never published to React, so there is no gesture state
    // to clear. Its following click remains the one selection activation.
    if (next.phase === "idle") return;
    setDirectGesture(next);
    submitDirect(next);
  };
  const cancelDirect = (reason: string) => {
    const current = directGestureRef.current;
    if (current.phase !== "dragging" && current.phase !== "keyboard_draft")
      return;
    stopAutoScroll();
    releaseCapture();
    const next = reduceNleTimelineGesture(current, { type: "cancel", reason });
    directGestureRef.current = next;
    if (
      current.phase === "dragging" &&
      current.draft.kind === "insert_from_bin"
    )
      binInsert?.cancel(reason, current.draft.pointerId ?? undefined);
    if (publishedDirectGesture.current !== next) setDirectGesture(next);
  };
  const clickClip = (
    clip: CompositionClip,
    mode: "replace" | "toggle" | "range",
  ) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    select(clip, mode);
  };
  const beginDirectKeyboard = (
    clip: CompositionClip,
    input: "keyboard" | "click",
  ) => {
    const movingIds = selected.has(clip.clipId) ? selection : [clip.clipId];
    const origins = snapshot.clips.filter((member) =>
      movingIds.includes(member.clipId),
    );
    return reduceNleTimelineGesture(directGestureRef.current, {
      type: "begin_move",
      input,
      identity: gestureIdentity(movingIds),
      pointerId: null,
      originClientX: 0,
      originClientY: 0,
      pixelsPerFrame,
      origins: origins.map((member) => ({
        clipId: member.clipId,
        trackId: member.trackId,
        startFrame: member.startFrame,
      })),
      trackIds: tracks.map((track) => track.trackId),
      targetTrackIds: origins.map((member) => member.trackId),
    });
  };
  const stepDirectKeyboard = (
    current: NleTimelineGestureState,
    deltaFrames: number,
    rowDelta: number,
  ) => {
    if (current.phase !== "keyboard_draft") return current;
    const origins = current.draft.origins
      .map((origin) =>
        snapshot.clips.find((clip) => clip.clipId === origin.clipId),
      )
      .filter((clip): clip is CompositionClip => clip !== undefined);
    const sourceAnchor = tracks.findIndex(
      (track) => track.trackId === origins[0]?.trackId,
    );
    const targetAnchor = tracks.findIndex(
      (track) => track.trackId === current.draft.targetTrackIds[0],
    );
    const currentRowDelta =
      sourceAnchor < 0 || targetAnchor < 0 ? 0 : targetAnchor - sourceAnchor;
    const targets = targetTracksFor(origins, currentRowDelta + rowDelta);
    const nextDelta = current.draft.deltaFrames + deltaFrames;
    const admission = admitMove(origins, targets, nextDelta);
    return reduceNleTimelineGesture(current, {
      type: "step",
      deltaFrames,
      targetTrackIds: targets,
      ...admission,
    });
  };
  const clipKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    clip: CompositionClip,
  ) => {
    const current = directGestureRef.current;
    if (current.phase === "keyboard_draft") {
      if (!current.draft.identity.clipIds.includes(clip.clipId)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        cancelDirect("escape");
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        const next = reduceNleTimelineGesture(current, {
          type: "commit",
          requestId: `move-k-${Date.now()}`,
        });
        setDirectGesture(next);
        submitDirect(next);
        return;
      }
      const horizontal =
        event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
      const vertical =
        event.altKey && event.key === "ArrowUp"
          ? -1
          : event.altKey && event.key === "ArrowDown"
            ? 1
            : 0;
      if (horizontal === 0 && vertical === 0) return;
      event.preventDefault();
      event.stopPropagation();
      const step = event.shiftKey ? Math.max(1, gridFrames) : 1;
      setDirectGesture(
        stepDirectKeyboard(current, horizontal * step, vertical),
      );
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      const next = beginDirectKeyboard(clip, "keyboard");
      setDirectGesture(next);
      return;
    }
    if (event.key === " " || event.code === "Space") {
      event.preventDefault();
      event.stopPropagation();
      select(clip, event.ctrlKey || event.metaKey ? "toggle" : "replace");
      return;
    }
    const direction =
      event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : null;
    const endpoint = event.key === "Home" ? 0 : event.key === "End" ? -1 : null;
    if (direction === null && endpoint === null) return;
    event.preventDefault();
    event.stopPropagation();
    const ordered = [...orderedNavigationClips].sort(
      (left, right) =>
        left.trackOrder - right.trackOrder ||
        left.startFrame - right.startFrame ||
        left.clipId.localeCompare(right.clipId),
    );
    const targetId =
      endpoint === null
        ? adjacentClipId(ordered, clip.clipId, direction!)
        : (ordered.at(endpoint)?.clipId ?? null);
    const target = snapshot.clips.find((member) => member.clipId === targetId);
    if (target === undefined) return;
    pendingFocusClipId.current = target.clipId;
    revealClip(target);
    if (event.shiftKey) select(target, "range");
  };
  const applyClickMove = () => {
    if (clickMove === null) return;
    const clip = snapshot.clips.find(
      (member) => member.clipId === clickMove.clipId,
    );
    if (clip === undefined) return;
    let next = beginDirectKeyboard(clip, "click");
    next = stepDirectKeyboard(next, clickMove.deltaFrames, clickMove.rowDelta);
    if (next.phase === "keyboard_draft" && !next.draft.admitted) {
      setDirectGesture(next);
      return;
    }
    next = reduceNleTimelineGesture(next, {
      type: "commit",
      requestId: `move-c-${Date.now()}`,
    });
    setClickMove(null);
    setDirectGesture(next);
    submitDirect(next);
  };
  useEffect(() => () => stopAutoScroll(), []);
  return {
    directGesture,
    setDirectGesture,
    directGestureRef,
    clickMove,
    setClickMove,
    autoScrollMappingRef,
    stopAutoScroll,
    beginMove,
    moveDirect,
    releaseDirect,
    cancelDirect,
    clickClip,
    beginDirectKeyboard,
    stepDirectKeyboard,
    clipKeyDown,
    submitDirect,
    applyClickMove,
  };
}
