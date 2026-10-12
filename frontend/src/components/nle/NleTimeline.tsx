import { NleTimelineRollAlternative } from "./NleTimelineRollAlternative";
import { NleTimelineTrimRail } from "./NleTimelineTrimRail";
import { NleTimelinePlayhead } from "./NleTimelinePlayhead";
import { NleTimelineScrollBar } from "./NleTimelineScrollBar";
import { NleTimelineTracks } from "./NleTimelineTracks";
import { NleTimelineRuler } from "./NleTimelineRuler";
import { NleTimelineNavigation } from "./NleTimelineNavigation";
import { useNleTimelineInteraction } from "./useNleTimelineInteraction";
import { useNleTimelineTools } from "./useNleTimelineTools";
import { useNleTimelineRoll } from "./useNleTimelineRoll";
import { useNleTimelineDirectGesture } from "./useNleTimelineDirectGesture";
import { useNleTimelineSelection } from "./useNleTimelineSelection";
import { timelineAuthoringContext } from "./nleTimelineTypes";
import { useNleTimelineCapture } from "./useNleTimelineCapture";
import { useNleTimelineTrim } from "./useNleTimelineTrim";
import { useNleTimelineTransport } from "./useNleTimelineTransport";
import { useNleTimelineDecorations } from "./useNleTimelineDecorations";
//  virtualized multi-track timeline with `NLE-EDGE-TRIM-V1` edge handles.
//
// Rows and clips are windowed: only tracks inside the scroll viewport (plus one on each side)
// and clips intersecting the visible frame window mount, bounded to 8 rows and 96 clip nodes.
// Zoom/scroll/scrub are `local_transport`; clip selection issues the accepted `select_clips`;
// a committed edge drag issues exactly one `trim_clip` transaction and one undo step.

import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";

import type { CompositionClip } from "../../contracts/compositionCodec";
import { LANE_LEAD_IN_PX } from "../../runtime/timelineGeometry";
import { fill, nleCopy } from "./nleCopy";
import {
  clipEditAffordances,
  moveRefusalText,
  assetDisplayNames,
  clipDisplayName,
  trackDisplayNames,
} from "./nleTimelineSurface";
import { NleClipMenu, NleTrackMenu } from "./NleTimelineMenus";

import { useNleTimelineViewport } from "./useNleTimelineViewport";
import {
  NLE_ROW_HEIGHT_PX,
  NLE_MAX_VISIBLE_ROWS,
  NLE_MAX_CLIP_NODES,
  type NleTimelineProps,
  type NleTimelineShortcutHandler,
} from "./nleTimelineTypes";
export {
  NLE_ROW_HEIGHT_PX,
  NLE_MAX_VISIBLE_ROWS,
  NLE_MAX_CLIP_NODES,
} from "./nleTimelineTypes";
export type {
  NleTimelineProps,
  NleTimelineShortcutHandler,
  NleTimelineTransportChannel,
  NleTimelineTransportSnapshot,
} from "./nleTimelineTypes";

export function NleTimeline({
  locale,
  snapshot,
  selection,
  authoring,
  gridFrames,
  editCapacityFrames,
  contentEndExclusive,
  playheadFrame,
  transportAvailable = true,
  navigationAvailable = true,
  transportChannel,
  highlightedAssetIds,
  onIntent,
  onEdgeGestureActive,
  onTrimState,
  onBeginTrim,
  onSeek,
  onBeginScrub,
  onTogglePlay,
  onStep,
  bindShortcuts,
  rebaseAttempt = null,
  retention,
  binInsert,
  decorationDemandBroker,
  leaseClient,
  runtimeEpoch = 0,
}: NleTimelineProps) {
  const text = nleCopy(locale);
  const context = timelineAuthoringContext(authoring, snapshot);
  const {
    timelineAuthority,
    authoringFingerprint,
    capturedAuthoringIdentity,
    timelineHistoryV2,
    receiptId,
    rejection,
  } = context;
  const transportFrameReaderRef = useRef<() => number | null>(
    () => playheadFrame,
  );
  const paintRequestRef = useRef<() => void>(() => undefined);
  const [focusedClipId, setFocusedClipId] = useState<string | null>(
    selection[0] ?? null,
  );
  const {
    scrollRef,
    timelineView,
    setTimelineView,
    pixelsPerFrame,
    fitLocked,
    viewStart,
    snapEnabled,
    setSnapEnabled,
    rippleEnabled,
    setRippleEnabled,
    scrollTop,
    setScrollTop,
    laneWidth,
    laneWidthRef,
    virtualizationHeight,
    measuredViewportHeightRef,
    durationFrames,
    durationFramesRef,
    transportEndExclusive,
    transportLastFrame,
    fps,
    tracks,
    coarse,
    headerWidth,
    headerWidthRef,
    viewStartRef,
    pixelsPerFrameRef,
    scrollDragRef,
    mappingKey,
    scaleBounds,
    totalRows,
    firstRow,
    lastRow,
    visibleTracks,
    visibleFrames,
    viewEnd,
    scrollBarInput,
    scrollBar,
    scrollToThumb,
    clipsByTrack,
    mountedClipIds,
    visibleCandidates,
    zoomBy,
    fit,
    zoomSelection,
    zoomPosition,
    setZoomPosition,
  } = useNleTimelineViewport({
    snapshot,
    selection,
    retention,
    editCapacityFrames,
    contentEndExclusive,
    focusedClipId,
    readPlayheadFrame: () => transportFrameReaderRef.current(),
    requestPaint: () => paintRequestRef.current(),
  });
  const {
    rulerRef,
    previousFrameRef,
    nextFrameRef,
    playheadElementRef,
    playheadFrameRef,
    transportAvailableRef,
    navigationAvailableRef,
    scrubRef,
    ruler,
    rulerMinorPath,
    rulerFrame,
    rulerRequestedFrameRef,
    rulerClearReasonRef,
    rulerAcknowledgedGenerationRef,
    renderedToolKeyRef,
    currentToolKeyRef,
    queueRulerSeek,
    flushRulerSeek,
    beginRulerScrub,
    rulerSeek,
    cancelScrub,
  } = useNleTimelineTransport({
    snapshot,
    playheadFrame,
    transportAvailable,
    navigationAvailable,
    transportChannel,
    onSeek,
    onBeginScrub,
    durationFrames,
    fps,
    laneWidth,
    pixelsPerFrame,
    viewStart,
    viewStartRef,
    pixelsPerFrameRef,
    headerWidthRef,
    transportEndExclusive,
    transportLastFrame,
  });
  transportFrameReaderRef.current = () => playheadFrameRef.current;
  const {
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
  } = useNleTimelineSelection({
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
  });
  const { captureRef, releaseCapture } = useNleTimelineCapture();
  const draftable =
    authoring.status === "ready" || authoring.status === "conflict";
  const {
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
  } = useNleTimelineTrim({
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
  });
  const {
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
  } = useNleTimelineDirectGesture({
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
  });
  const {
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
  } = useNleTimelineRoll({
    snapshot,
    selection,
    gridFrames,
    onBeginTrim,
    pixelsPerFrameRef,
    draftable,
    isCompetingDraftActive: () =>
      gestureRef.current.phase === "dragging" ||
      gestureRef.current.phase === "keyboard_draft" ||
      directGestureRef.current.phase === "dragging" ||
      directGestureRef.current.phase === "keyboard_draft",
    onRunTool: (action) => {
      runTool(action);
    },
  });
  //  (R5): display names replace raw ids in every visible text, name and tooltip.
  const trackNames = useMemo(
    () => trackDisplayNames(snapshot.tracks, text.timeline.trackNames),
    [snapshot.tracks, text],
  );
  const assetNames = useMemo(
    () => assetDisplayNames(snapshot.assets, text.assets.card),
    [snapshot.assets, text],
  );
  const clipName = useCallback(
    (clip: CompositionClip) =>
      clipDisplayName(clip, assetNames, {
        cardTemplate: text.assets.card,
        titleClip: text.timeline.titleClip,
      }),
    [assetNames, text],
  );
  const railClip =
    selectedClip !== undefined &&
    clipEditAffordances({
      width: Math.max(20, selectedClip.durationFrames * pixelsPerFrame),
      selected: true,
      coarse,
      draftEdge: null,
    }).rail
      ? selectedClip
      : undefined;
  const highlighted = new Set(highlightedAssetIds);
  const draft =
    gesture.phase === "dragging" || gesture.phase === "keyboard_draft"
      ? gesture.draft
      : null;
  // CRITICAL: virtualized removal is not a blur. When a row scrolls out of the window React
  // unmounts its grips without dispatching `focusout`, so the `onGripBlur` binding never runs:
  // an uncommitted draft -- and with it the overlay's edge-first Escape guard -- would stay
  // armed on a control that no longer exists, while focus falls to `document.body` outside the
  // dialog where its own key handler can never see Escape (post-corrective review 02, R2-F3).
  // Ownership is therefore checked against the mounted window, not against focus, and against
  // *mount* rather than `showGrips`: a draft that trims its own clip below the inline threshold
  // only sets `hidden` on a still-mounted button, which does blur and is already handled, so
  // cancelling on that would regress the accepted shrink-during-draft behaviour. The rail
  // layout renders outside the scroll container and never unmounts on scroll. `submitting` and
  // `reconciling` own a real backend transaction and must survive the same scroll untouched.
  const maxViewStart = Math.max(0, durationFrames - visibleFrames);
  const gestureStatus =
    gesture.phase === "submitting"
      ? text.timeline.trimSubmitting
      : gesture.phase === "accepted"
        ? text.timeline.trimAccepted
        : gesture.phase === "rejected"
          ? text.timeline.trimRejected
          : gesture.phase === "reconciling"
            ? text.timeline.trimReconciling
            : draft !== null
              ? fill(text.timeline.trimDraft, {
                  edge:
                    draft.identity.edge === "start"
                      ? text.timeline.edgeStart
                      : text.timeline.edgeEnd,
                  delta: draft.geometry.deltaFrames,
                })
              : "";
  const inserting =
    directGesture.phase !== "idle" &&
    directGesture.draft.kind === "insert_from_bin";
  const directStatus =
    directGesture.phase === "submitting"
      ? inserting
        ? text.timeline.insertSubmitting
        : text.timeline.moveSubmitting
      : directGesture.phase === "accepted"
        ? inserting
          ? text.timeline.insertAccepted
          : text.timeline.moveAccepted
        : directGesture.phase === "rejected"
          ? inserting
            ? text.timeline.insertRejected
            : text.timeline.moveRejected
          : directGesture.phase === "reconciling"
            ? inserting
              ? text.timeline.insertReconciling
              : text.timeline.moveReconciling
            : directGesture.phase === "dragging" ||
                directGesture.phase === "keyboard_draft"
              ? directGesture.draft.admitted
                ? fill(
                    inserting
                      ? text.timeline.insertDraft
                      : text.timeline.moveDraft,
                    {
                      frame: directGesture.draft.insert?.targetFrame ?? 0,
                      delta: directGesture.draft.deltaFrames,
                    },
                  )
                : fill(
                    inserting
                      ? text.timeline.insertRefused
                      : text.timeline.moveRefused,
                    {
                      reason: moveRefusalText(
                        text.timeline.moveRefusals,
                        directGesture.draft.reason,
                      ),
                    },
                  )
              : "";

  const { decide, toolDecisions, runTool, shortcutHandlerRef } =
    useNleTimelineTools({
      snapshot,
      selection,
      authoring,
      onIntent,
      rebaseAttempt,
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
    });

  const {
    marquee,
    gridKeyDown,
    gridPointerDown,
    gridPointerMove,
    gridPointerUp,
    gridPointerCancel,
  } = useNleTimelineInteraction({
    snapshot,
    selection,
    onIntent,
    onTrimState,
    onEdgeGestureActive,
    draft,
    railClip,
    releaseCapture,
    mappingKey,
    mountedClipIds,
    scrollRef,
    headerWidthRef,
    tracks,
    viewStart,
    pixelsPerFrame,
    gesture,
    gestureRef,
    setGesture,
    cancelPointer,
    directGestureRef,
    autoScrollMappingRef,
    cancelDirect,
    rollDraft,
    rollDraftRef,
    setRollDraft,
    cancelRoll,
    pendingFocusClipId,
    shortcutHandlerRef,
  });
  const { canvasRef: decorationRef, requestPaint } = useNleTimelineDecorations({
    snapshot,
    runtimeEpoch,
    leaseClient,
    decorationDemandBroker,
    visibleCandidates,
    directGesture,
    tracks,
    firstRow,
    laneWidth,
    measuredViewportHeightRef,
    pixelsPerFrame,
    viewStart,
    marquee,
    ruler,
    selected,
  });
  paintRequestRef.current = requestPaint;

  return (
    <section
      className="h3-nle-timeline"
      aria-label={text.timeline.title}
      data-h3-nle-region="timeline"
      data-h3-nle-pixels-per-frame={pixelsPerFrame}
      data-h3-nle-view-start={viewStart}
      data-h3-nle-lane-origin-px={headerWidth + LANE_LEAD_IN_PX}
      style={
        {
          "--h3-nle-track-header-width": `${headerWidth}px`,
        } as CSSProperties
      }
    >
      <NleTimelineNavigation
        locale={locale}
        selection={selection}
        authoring={authoring}
        gridFrames={gridFrames}
        onSeek={onSeek}
        onBeginScrub={onBeginScrub}
        setTimelineView={setTimelineView}
        pixelsPerFrame={pixelsPerFrame}
        fitLocked={fitLocked}
        viewStart={viewStart}
        snapEnabled={snapEnabled}
        setSnapEnabled={setSnapEnabled}
        rippleEnabled={rippleEnabled}
        setRippleEnabled={setRippleEnabled}
        transportLastFrame={transportLastFrame}
        scaleBounds={scaleBounds}
        totalRows={totalRows}
        firstRow={firstRow}
        lastRow={lastRow}
        zoomBy={zoomBy}
        fit={fit}
        zoomSelection={zoomSelection}
        zoomPosition={zoomPosition}
        setZoomPosition={setZoomPosition}
        previousFrameRef={previousFrameRef}
        nextFrameRef={nextFrameRef}
        playheadFrameRef={playheadFrameRef}
        navigationAvailableRef={navigationAvailableRef}
        cancelPointer={cancelPointer}
        directGesture={directGesture}
        clickMove={clickMove}
        setClickMove={setClickMove}
        cancelDirect={cancelDirect}
        applyClickMove={applyClickMove}
        setRollDraft={setRollDraft}
        rollCuts={rollCuts}
        clickSelectionMode={clickSelectionMode}
        setClickSelectionMode={setClickSelectionMode}
        orderedNavigationClips={orderedNavigationClips}
        selectAdjacent={selectAdjacent}
        replaceSelection={replaceSelection}
        toolDecisions={toolDecisions}
        runTool={runTool}
        text={text}
        maxViewStart={maxViewStart}
        focusedClipId={focusedClipId}
      />
      {tracks.length === 0 ? (
        <button
          type="button"
          className="h3-nle-menu-launcher"
          data-h3-nle-menu-trigger="track"
          aria-haspopup="menu"
          aria-expanded={trackMenuTarget !== null}
          aria-label={menuLabels.openTrack}
          onClick={(event) => openTrackMenu(event, null)}
        >
          ⋯
        </button>
      ) : null}
      {/* the ruler, the tracks and the scroll bar are one surface with no gap, so the
          playhead layer can span from the ruler top to the lane bottom outside the scroller. */}
      <div className="h3-nle-timeline-surface">
        <NleTimelineRuler
          gridFrames={gridFrames}
          onBeginScrub={onBeginScrub}
          pixelsPerFrame={pixelsPerFrame}
          viewStart={viewStart}
          laneWidth={laneWidth}
          transportLastFrame={transportLastFrame}
          fps={fps}
          rulerRef={rulerRef}
          playheadFrameRef={playheadFrameRef}
          navigationAvailableRef={navigationAvailableRef}
          scrubRef={scrubRef}
          ruler={ruler}
          rulerMinorPath={rulerMinorPath}
          rulerFrame={rulerFrame}
          rulerRequestedFrameRef={rulerRequestedFrameRef}
          queueRulerSeek={queueRulerSeek}
          flushRulerSeek={flushRulerSeek}
          beginRulerScrub={beginRulerScrub}
          rulerSeek={rulerSeek}
          cancelScrub={cancelScrub}
          text={text}
          // GUARD: the ruler is memoized, so it re-renders only when a prop changes. These refs are
          // read here, in the same render pass the ruler used to read them in; passing the refs alone
          // would leave its clear reason, acknowledged generation and tab stop at stale values.
          navigationAvailable={navigationAvailableRef.current}
          requestClearReason={rulerClearReasonRef.current}
          requestGeneration={rulerAcknowledgedGenerationRef.current}
        />
        <NleTimelineTracks
          draftable={draftable}
          locale={locale}
          snapshot={snapshot}
          selection={selection}
          authoring={authoring}
          onIntent={onIntent}
          onSeek={onSeek}
          onBeginScrub={onBeginScrub}
          scrollRef={scrollRef}
          pixelsPerFrame={pixelsPerFrame}
          viewStart={viewStart}
          scrollTop={scrollTop}
          setScrollTop={setScrollTop}
          durationFrames={durationFrames}
          fps={fps}
          tracks={tracks}
          coarse={coarse}
          headerWidth={headerWidth}
          totalRows={totalRows}
          firstRow={firstRow}
          lastRow={lastRow}
          visibleTracks={visibleTracks}
          viewEnd={viewEnd}
          clipsByTrack={clipsByTrack}
          mountedClipIds={mountedClipIds}
          navigationAvailableRef={navigationAvailableRef}
          gesture={gesture}
          gestureRef={gestureRef}
          beginPointer={beginPointer}
          movePointer={movePointer}
          releasePointer={releasePointer}
          cancelPointer={cancelPointer}
          gripKeyDown={gripKeyDown}
          directGesture={directGesture}
          directGestureRef={directGestureRef}
          beginMove={beginMove}
          moveDirect={moveDirect}
          releaseDirect={releaseDirect}
          cancelDirect={cancelDirect}
          clickClip={clickClip}
          clipKeyDown={clipKeyDown}
          rollDraft={rollDraft}
          setRollDraft={setRollDraft}
          rollDraftRef={rollDraftRef}
          suppressRollClickRef={suppressRollClickRef}
          selectedClip={selectedClip}
          rollCuts={rollCuts}
          cancelRoll={cancelRoll}
          beginRollPointer={beginRollPointer}
          moveRollPointer={moveRollPointer}
          releaseRollPointer={releaseRollPointer}
          rollKeyDown={rollKeyDown}
          clickSelectionMode={clickSelectionMode}
          openTrackMenu={openTrackMenu}
          openClipMenu={openClipMenu}
          openClipMenuFromKey={openClipMenuFromKey}
          selected={selected}
          decide={decide}
          gridKeyDown={gridKeyDown}
          gridPointerDown={gridPointerDown}
          gridPointerMove={gridPointerMove}
          gridPointerUp={gridPointerUp}
          gridPointerCancel={gridPointerCancel}
          text={text}
          trackNames={trackNames}
          clipName={clipName}
          highlighted={highlighted}
          draft={draft}
          decorationRef={decorationRef}
        />
        <NleTimelineScrollBar
          tracks={tracks}
          scrollDragRef={scrollDragRef}
          scrollBarInput={scrollBarInput}
          scrollBar={scrollBar}
          scrollToThumb={scrollToThumb}
          ruler={ruler}
        />
        <NleTimelinePlayhead
          pixelsPerFrame={pixelsPerFrame}
          viewStart={viewStart}
          viewEnd={viewEnd}
          rulerRef={rulerRef}
          playheadElementRef={playheadElementRef}
          playheadFrame={playheadFrameRef.current}
          navigationAvailableRef={navigationAvailableRef}
          scrubRef={scrubRef}
          ruler={ruler}
          flushRulerSeek={flushRulerSeek}
          beginRulerScrub={beginRulerScrub}
          rulerSeek={rulerSeek}
          cancelScrub={cancelScrub}
        />
        {/* The gesture outcomes stay live regions with their attributes (R6); they float over the
          lane bottom instead of taking a footer row the canvas does not have. */}
        <div className="h3-nle-timeline-feedback">
          <output
            role="status"
            aria-live="polite"
            data-h3-nle-status="trim"
            data-h3-nle-trim-phase={gesture.phase}
            data-code={
              gesture.phase === "rejected" ? gesture.reason : undefined
            }
          >
            {gestureStatus}
          </output>
          <output
            role="status"
            aria-live="polite"
            data-h3-nle-status="move"
            data-h3-nle-move-phase={directGesture.phase}
            data-code={
              directGesture.phase === "rejected"
                ? directGesture.reason
                : directGesture.phase === "dragging" ||
                    directGesture.phase === "keyboard_draft"
                  ? (directGesture.draft.reason ?? undefined)
                  : undefined
            }
          >
            {directStatus}
          </output>
        </div>
      </div>
      <p className="h3-nle-vh" id="h3-nle-trim-instructions">
        {text.timeline.trimInstructions}
      </p>
      <NleTrackMenu
        locale={locale}
        snapshot={snapshot}
        target={trackMenuTarget}
        busy={authoring.status === "pending" || authoring.status === "loading"}
        onIntent={onIntent}
        onClose={() => setTrackMenuTarget(null)}
      />
      <NleClipMenu
        locale={locale}
        snapshot={snapshot}
        target={clipMenuTarget}
        busy={authoring.status === "pending" || authoring.status === "loading"}
        rippleEnabled={rippleEnabled}
        onIntent={onIntent}
        onClose={() => setClipMenuTarget(null)}
      />
      <NleTimelineTrimRail
        draftable={draftable}
        locale={locale}
        snapshot={snapshot}
        durationFrames={durationFrames}
        fps={fps}
        tracks={tracks}
        gesture={gesture}
        beginPointer={beginPointer}
        movePointer={movePointer}
        releasePointer={releasePointer}
        cancelPointer={cancelPointer}
        gripKeyDown={gripKeyDown}
        openClipMenu={openClipMenu}
        text={text}
        clipName={clipName}
        railClip={railClip}
        draft={draft}
      />
      <NleTimelineRollAlternative
        rollDraft={rollDraft}
        setRollDraft={setRollDraft}
        cancelRoll={cancelRoll}
        commitRoll={commitRoll}
        text={text}
      />
    </section>
  );
}
