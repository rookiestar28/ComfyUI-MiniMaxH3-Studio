import { useCallback, useLayoutEffect, useMemo, useRef } from "react";
import type { FilmstripPaintInput } from "../../runtime/nleFilmstripPainter";
import {
  isNleSchedulerTraceEnabled,
  type WaveformPaintInput,
} from "../../runtime/nleWaveformPainter";
import {
  createTimelineRasterScheduler,
  type TimelineDecorationScene,
} from "../../runtime/nleTimelineRaster";
import {
  LANE_LEAD_IN_PX,
  laneXForBoundary,
  rulerMarks,
} from "../../runtime/timelineGeometry";
import type { NleTimelineGestureState } from "../../runtime/nleTimelineGesture";
import { useNleFilmstrips } from "./useNleFilmstrips";
import { useNleWaveforms } from "./useNleWaveforms";
import type { useNleTimelineViewport } from "./useNleTimelineViewport";
import {
  NLE_ROW_HEIGHT_PX,
  NLE_MAX_VISIBLE_ROWS,
  CLIP_CONTENT_TOP_PX,
  CLIP_CONTENT_HEIGHT_PX,
  WAVEFORM_BAND_HEIGHT_PX,
  type NleTimelineProps,
} from "./nleTimelineTypes";

type DecorationInput = Pick<
  NleTimelineProps,
  "snapshot" | "runtimeEpoch" | "leaseClient" | "decorationDemandBroker"
> &
  Pick<
    ReturnType<typeof useNleTimelineViewport>,
    | "visibleCandidates"
    | "tracks"
    | "firstRow"
    | "laneWidth"
    | "measuredViewportHeightRef"
    | "pixelsPerFrame"
    | "viewStart"
  > &
  Readonly<{
    directGesture: NleTimelineGestureState;
    marquee: TimelineDecorationScene["marquee"];
    ruler: ReturnType<typeof rulerMarks>;
    selected: ReadonlySet<string>;
  }>;

export function useNleTimelineDecorations({
  snapshot,
  runtimeEpoch = 0,
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
}: DecorationInput) {
  const decorationRef = useRef<HTMLCanvasElement>(null);
  const rasterRef = useRef<ReturnType<
    typeof createTimelineRasterScheduler
  > | null>(null);
  const frameRate = snapshot.output.frameRate as
    Readonly<{ num: number; den: number }> | undefined;
  const visibleVideoAssetIds = useMemo(() => {
    const videoAssets = new Set(
      snapshot.assets
        .filter((asset) => asset.kind === "video")
        .map((asset) => asset.assetId),
    );
    return [
      ...new Set(
        visibleCandidates.flatMap((clip) =>
          clip.assetId !== null && videoAssets.has(clip.assetId)
            ? [clip.assetId]
            : [],
        ),
      ),
    ];
  }, [snapshot.assets, visibleCandidates]);
  const filmstrips = useNleFilmstrips({
    snapshot,
    runtimeEpoch,
    leaseClient,
    decorationDemandBroker,
    visibleAssetIds: visibleVideoAssetIds,
  });
  const waveforms = useNleWaveforms({
    snapshot,
    runtimeEpoch,
    leaseClient,
    decorationDemandBroker,
    visibleAssetIds: visibleVideoAssetIds,
  });
  const decorationScene = useCallback((): TimelineDecorationScene => {
    const directDraft =
      directGesture.phase === "dragging" ||
      directGesture.phase === "keyboard_draft"
        ? directGesture.draft
        : null;
    const moving = directDraft?.origins[0];
    const movingClip = snapshot.clips.find(
      (clip) => clip.clipId === moving?.clipId,
    );
    const movingTrackIndex = movingClip
      ? tracks.findIndex((track) => track.trackId === movingClip.trackId)
      : -1;
    const insert = directDraft?.insert ?? null;
    const insertTrackIndex =
      insert?.targetTrackId === null || insert?.targetTrackId === undefined
        ? -1
        : tracks.findIndex((track) => track.trackId === insert.targetTrackId);
    if (isNleSchedulerTraceEnabled()) {
      const target = window as Window & {
        __h3NleWaveformSceneTrace?: Array<Readonly<Record<string, unknown>>>;
      };
      const trace = (target.__h3NleWaveformSceneTrace ??= []);
      for (const clip of visibleCandidates) {
        const track = tracks.find((item) => item.trackId === clip.trackId);
        const asset = snapshot.assets.find(
          (item) => item.assetId === clip.assetId,
        );
        const envelope =
          clip.assetId === null ? undefined : waveforms.get(clip.assetId);
        const accepted =
          track?.kind === "primary_video" &&
          track.enabled &&
          asset?.kind === "video" &&
          asset.embeddedAudio === "present_bound" &&
          asset.sourceTimeBase !== null &&
          asset.sourceFrameCount !== null &&
          asset.sourceSampleCount !== null &&
          envelope !== undefined;
        trace.push({
          event: "waveform.scene.candidate",
          clipId: clip.clipId,
          assetId: clip.assetId,
          accepted,
          trackKind: track?.kind ?? null,
          trackEnabled: track?.enabled ?? null,
          assetKind: asset?.kind ?? null,
          embeddedAudio: asset?.embeddedAudio ?? null,
          sourceTimeBase: asset?.sourceTimeBase ?? null,
          sourceFrameCount: asset?.sourceFrameCount ?? null,
          sourceSampleCount: asset?.sourceSampleCount ?? null,
          envelopePresent: envelope !== undefined,
          pairCount: envelope?.pairCount ?? null,
          pairLength: envelope?.pairs.length ?? null,
        });
      }
      if (trace.length > 256) trace.splice(0, trace.length - 256);
    }
    return {
      width: laneWidth + LANE_LEAD_IN_PX,
      height: Math.min(
        measuredViewportHeightRef.current,
        NLE_MAX_VISIBLE_ROWS * NLE_ROW_HEIGHT_PX,
      ),
      filmstrips: visibleCandidates.flatMap<FilmstripPaintInput>((clip) => {
        const asset = snapshot.assets.find(
          (item) => item.assetId === clip.assetId,
        );
        const decoration =
          clip.assetId === null ? undefined : filmstrips.get(clip.assetId);
        if (
          asset?.kind !== "video" ||
          asset.sourceTimeBase === null ||
          asset.sourceFrameCount === null ||
          decoration === undefined ||
          frameRate === undefined
        )
          return [];
        return [
          {
            asset: {
              ...asset,
              kind: "video",
              sourceTimeBase: asset.sourceTimeBase,
              sourceFrameCount: asset.sourceFrameCount,
              sourceWidth: decoration.sourceWidth,
              sourceHeight: decoration.sourceHeight,
            },
            outputFrameRate: frameRate,
            clip,
            bounds: {
              x: laneXForBoundary(clip.startFrame, viewStart, pixelsPerFrame),
              y:
                (tracks.findIndex((track) => track.trackId === clip.trackId) -
                  firstRow) *
                  NLE_ROW_HEIGHT_PX +
                CLIP_CONTENT_TOP_PX,
              width: Math.max(1, clip.durationFrames * pixelsPerFrame),
              height: CLIP_CONTENT_HEIGHT_PX,
            },
            visible: { x: 0, width: laneWidth + LANE_LEAD_IN_PX },
            sprite: decoration,
          },
        ];
      }),
      waveforms: visibleCandidates.flatMap<WaveformPaintInput>((clip) => {
        const track = tracks.find((item) => item.trackId === clip.trackId);
        const asset = snapshot.assets.find(
          (item) => item.assetId === clip.assetId,
        );
        const envelope =
          clip.assetId === null ? undefined : waveforms.get(clip.assetId);
        if (
          track?.kind !== "primary_video" ||
          !track.enabled ||
          asset?.kind !== "video" ||
          asset.embeddedAudio !== "present_bound" ||
          asset.sourceTimeBase === null ||
          asset.sourceFrameCount === null ||
          asset.sourceSampleCount === null ||
          envelope === undefined ||
          frameRate === undefined
        )
          return [];
        const clipY =
          (tracks.findIndex((track) => track.trackId === clip.trackId) -
            firstRow) *
            NLE_ROW_HEIGHT_PX +
          CLIP_CONTENT_TOP_PX;
        const clipHeight = CLIP_CONTENT_HEIGHT_PX;
        return [
          {
            asset: {
              ...asset,
              kind: "video",
              embeddedAudio: "present_bound",
              sourceTimeBase: asset.sourceTimeBase,
              sourceFrameCount: asset.sourceFrameCount,
              sourceSampleCount: asset.sourceSampleCount,
            },
            outputFrameRate: frameRate,
            clip,
            bounds: {
              x: laneXForBoundary(clip.startFrame, viewStart, pixelsPerFrame),
              y: clipY + clipHeight - WAVEFORM_BAND_HEIGHT_PX,
              width: Math.max(1, clip.durationFrames * pixelsPerFrame),
              height: WAVEFORM_BAND_HEIGHT_PX,
            },
            visible: { x: 0, width: laneWidth + LANE_LEAD_IN_PX },
            envelope,
          },
        ];
      }),
      // resolved at paint time, so the light and dark palettes both apply. Empty where
      // the overlay's role tokens are not mounted; the painter then keeps its own default.
      waveformColor:
        (decorationRef.current === null
          ? ""
          : getComputedStyle(decorationRef.current)
              .getPropertyValue("--h3-nle-waveform")
              .trim()) || undefined,
      marquee,
      snapX:
        directDraft?.snapFrame === null || directDraft?.snapFrame === undefined
          ? null
          : laneXForBoundary(directDraft.snapFrame, viewStart, pixelsPerFrame),
      ghost:
        directDraft !== null &&
        directDraft.kind === "insert_from_bin" &&
        insert !== null
          ? {
              x: laneXForBoundary(
                insert.targetFrame,
                viewStart,
                pixelsPerFrame,
              ),
              y:
                Math.max(0, insertTrackIndex - firstRow) * NLE_ROW_HEIGHT_PX +
                CLIP_CONTENT_TOP_PX,
              width: Math.max(1, insert.durationFrames * pixelsPerFrame),
              height: CLIP_CONTENT_HEIGHT_PX,
              admitted: directDraft.admitted,
            }
          : directDraft === null || movingClip === undefined
            ? null
            : {
                x: laneXForBoundary(
                  movingClip.startFrame + directDraft.deltaFrames,
                  viewStart,
                  pixelsPerFrame,
                ),
                y:
                  Math.max(0, movingTrackIndex - firstRow) * NLE_ROW_HEIGHT_PX +
                  CLIP_CONTENT_TOP_PX,
                width: Math.max(1, movingClip.durationFrames * pixelsPerFrame),
                height: CLIP_CONTENT_HEIGHT_PX,
                admitted: directDraft.admitted,
              },
    };
  }, [
    directGesture,
    filmstrips,
    firstRow,
    frameRate,
    laneWidth,
    marquee,
    pixelsPerFrame,
    ruler,
    selected,
    snapshot.clips,
    snapshot.assets,
    tracks,
    viewStart,
    visibleCandidates,
    waveforms,
  ]);
  useLayoutEffect(() => {
    const canvas = decorationRef.current;
    if (canvas === null) return;
    const scheduler = createTimelineRasterScheduler(canvas, decorationScene);
    rasterRef.current = scheduler;
    scheduler.request();
    return () => {
      // IMPORTANT: bitmap close does not erase copied pixels. Reset the backing store before
      // paint on scene/authority changes, so a revoked filmstrip never survives until the next RAF.
      scheduler.dispose();
      if (rasterRef.current === scheduler) rasterRef.current = null;
    };
  }, [decorationScene]);

  const requestPaint = useCallback(() => rasterRef.current?.request(), []);
  return { canvasRef: decorationRef, requestPaint };
}
