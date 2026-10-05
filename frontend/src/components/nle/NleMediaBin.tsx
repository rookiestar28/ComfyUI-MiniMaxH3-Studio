import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import type { AuthoringIntent } from "../../state/authoringViewState";
import { disposeAuthoringAudioPeaks } from "../../contracts/authoringAudioPeaks";
import {
  AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
  canonicalPublicRuntimeAssetFingerprint,
} from "../../contracts/authoringMediaLeaseCodec";
import type {
  PublicCompositionAsset,
  PublicCompositionSnapshot,
} from "../../contracts/compositionCodec";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaDecorationLeaseClient,
  AuthoringMediaSourceLeaseClient,
} from "../../host/authoringMediaSourceLease";
import type { NleDecorationDemandBroker } from "../../host/nleDecorationDemandBroker";
import {
  createNleLeaseScheduler,
  type NleLeaseScheduler,
} from "../../host/nleLeaseScheduler";
import type { Locale } from "../../i18n/catalog";
import { buildPublicAssetManifest } from "../../runtime/publicAssetManifest";
import { admitTimelineInsert } from "../../runtime/nleTimelineGesture";
import type { NleBinInsertChannel } from "../../runtime/nleBinInsertChannel";
import { createNleDecorationCache } from "../../host/nleDecorationCache";
import {
  build,
  freshIdentifier,
  type NewClipDraft,
  type PrimaryAppendDecision,
} from "./nleCommandBuilders";
import { NleAssetMenu, NleMediaSortFilterMenu } from "./NleBinMenus";
import { NleActionIcon } from "./NleIconActions";
import { fill, nleCopy, primaryAppendNotice } from "./nleCopy";
import {
  mediaBinProjection,
  mediaDurationPill,
  mediaDurationTimecode,
  mediaInsertionDurationFrames,
  selectMediaBinProjection,
  virtualMediaRange,
  type MediaFilter,
  type MediaSort,
} from "./nleMediaBinModel";
import type { TimelineMenuTarget } from "./NleTimelineMenus";
import { useNleBinCardDrag } from "./useNleBinCardDrag";

// M25-63 (R7): a card pitch of 148 px (a 140 px card plus the 8 px gap), so
// `columns = max(1, floor((inner + 8) / 148))`: two columns at the default share of a 1600 px
// overlay, one at the 160 px bin minimum. Each virtual row is 128 px: an 84 px thumbnail box, the
// name and the thumbnail status line, and the space before the next row. The grid's CSS
// (`grid-auto-rows`) must stay equal to `CARD_ROW_HEIGHT`, or the spacers drift from the rows.
const CARD_PITCH = 148;
const CARD_GAP = 8;
const CARD_ROW_HEIGHT = 128;

type Dispatch = (intent: AuthoringIntent) => Promise<void>;

function timelineDuration(snapshot: PublicCompositionSnapshot): number {
  const value = Number(snapshot.output.durationFrames);
  return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

function timelineFrameRate(
  snapshot: PublicCompositionSnapshot,
): Readonly<{ num: number; den: number }> | null {
  const rate = snapshot.output.frameRate as
    Readonly<{ num?: unknown; den?: unknown }> | undefined;
  const num = Number(rate?.num);
  const den = Number(rate?.den);
  return Number.isSafeInteger(num) &&
    Number.isSafeInteger(den) &&
    num > 0 &&
    den > 0
    ? { num, den }
    : null;
}

function insertionDuration(
  asset: PublicCompositionAsset,
  startFrame: number,
  totalFrames: number,
  frameRate: Readonly<{ num: number; den: number }> | null,
): number | null {
  const source = mediaInsertionDurationFrames(asset, frameRate);
  if (
    source === null ||
    !Number.isSafeInteger(source) ||
    source < 1 ||
    startFrame < 0 ||
    startFrame >= totalFrames
  )
    return null;
  return Math.min(source, totalFrames - startFrame);
}

/** Primary Add is append-only; the logical playhead and overlay extents never choose its start. */
export function admitPrimaryAppend(
  snapshot: PublicCompositionSnapshot,
  asset: PublicCompositionAsset,
): PrimaryAppendDecision {
  const primary = snapshot.tracks.find(
    (track) => track.kind === "primary_video" && track.enabled,
  );
  if (primary === undefined)
    return Object.freeze({ admitted: false, reason: "primary_missing" });
  if (primary.locked)
    return Object.freeze({ admitted: false, reason: "primary_locked" });
  const sourceDuration = mediaInsertionDurationFrames(
    asset,
    timelineFrameRate(snapshot),
  );
  if (
    sourceDuration === null ||
    !Number.isSafeInteger(sourceDuration) ||
    sourceDuration < 1
  )
    return Object.freeze({ admitted: false, reason: "timing_unavailable" });
  const startFrame = snapshot.clips
    .filter((clip) => clip.trackId === primary.trackId)
    .reduce(
      (end, clip) => Math.max(end, clip.startFrame + clip.durationFrames),
      0,
    );
  const totalFrames = timelineDuration(snapshot);
  if (startFrame + sourceDuration > totalFrames)
    return Object.freeze({
      admitted: false,
      reason: "insufficient_capacity",
    });
  const admission = admitTimelineInsert({
    asset,
    startFrame,
    durationFrames: sourceDuration,
    timelineDurationFrames: totalFrames,
    tracks: snapshot.tracks,
    clips: snapshot.clips,
    // CRITICAL: primary Add must pin the track already selected above. Letting admission pick
    // by display order can select an earlier video overlay and falsely reject a valid append.
    targetTrackId: primary.trackId,
    operation: "add",
  });
  if (!admission.admitted || admission.trackId !== primary.trackId)
    return Object.freeze({ admitted: false, reason: "append_rejected" });
  return Object.freeze({
    admitted: true,
    draft: Object.freeze({
      clipId: freshIdentifier(snapshot, "clip"),
      trackId: primary.trackId,
      assetId: asset.assetId,
      startFrame,
      durationFrames: sourceDuration,
      sourceStartFrame: 0,
      text: null,
    }),
  });
}

function insertionDraft(
  snapshot: PublicCompositionSnapshot,
  asset: PublicCompositionAsset,
  frame: number,
  operation: "add" | "insert_range" | "overwrite_range" = "add",
): NewClipDraft | null {
  const totalFrames = timelineDuration(snapshot);
  const startFrame = Math.max(0, Math.min(totalFrames - 1, Math.trunc(frame)));
  const durationFrames = insertionDuration(
    asset,
    startFrame,
    totalFrames,
    timelineFrameRate(snapshot),
  );
  if (durationFrames === null) return null;
  const admission = admitTimelineInsert({
    asset,
    startFrame,
    durationFrames,
    timelineDurationFrames: totalFrames,
    tracks: snapshot.tracks,
    clips: snapshot.clips,
    operation,
  });
  if (!admission.admitted || admission.trackId === null) return null;
  return Object.freeze({
    clipId: freshIdentifier(snapshot, "clip"),
    trackId: admission.trackId,
    assetId: asset.assetId,
    startFrame,
    durationFrames,
    sourceStartFrame: 0,
    text: null,
  });
}

function overwriteRemainderIds(
  snapshot: PublicCompositionSnapshot,
  draft: NewClipDraft,
): Readonly<Record<string, string>> {
  const result: Record<string, string> = {};
  const taken = new Set(snapshot.clips.map(({ clipId }) => clipId));
  let index = 1;
  for (const clip of snapshot.clips) {
    if (
      clip.trackId !== draft.trackId ||
      clip.startFrame >= draft.startFrame ||
      clip.startFrame + clip.durationFrames <=
        draft.startFrame + draft.durationFrames
    )
      continue;
    let identifier: string;
    do {
      identifier = `remainder-r${snapshot.timelineRevision}-${index++}`;
    } while (taken.has(identifier));
    taken.add(identifier);
    result[clip.clipId] = identifier;
  }
  return Object.freeze(result);
}

function BitmapCanvas({ bitmap }: { bitmap: ImageBitmap | undefined }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useLayoutEffect(() => {
    const canvas = ref.current;
    if (canvas === null) return;
    if (bitmap === undefined) {
      // IMPORTANT: closing an ImageBitmap does not erase pixels already copied to a canvas.
      // Reset the backing store before paint or a revoked private thumbnail stays visible.
      canvas.width = 0;
      canvas.height = 0;
      return;
    }
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
  }, [bitmap]);
  // B-M2563-08: the canvas carries the NLE canvas-kind marker, so the hardening census counts
  // thumbnails with the composition and decoration canvases (a leak of one is a violation).
  return (
    <canvas
      ref={ref}
      aria-hidden="true"
      data-h3-nle-thumbnail=""
      data-h3-nle-canvas="bin_thumbnail"
    />
  );
}

export function NleMediaBin({
  locale,
  snapshot,
  highlightedAssetIds,
  busy,
  runtimeEpoch,
  leaseClient,
  leaseScheduler,
  decorationCache,
  decorationDemandBroker,
  currentFrame,
  binInsert,
  authoringFingerprint,
  lead,
  onIntent,
}: {
  /** M25-63: the control that leads the bin's toolbar row (the editor's Import). */
  lead?: ReactNode;
  locale: Locale;
  snapshot: PublicCompositionSnapshot;
  highlightedAssetIds: readonly string[];
  busy: boolean;
  runtimeEpoch: number;
  leaseClient: AuthoringMediaSourceLeaseClient;
  leaseScheduler?: NleLeaseScheduler<AuthoringMediaAssetDecoration["value"]>;
  decorationCache?: ReturnType<typeof createNleDecorationCache<ImageBitmap>>;
  decorationDemandBroker?: NleDecorationDemandBroker<
    AuthoringMediaAssetDecoration["value"]
  >;
  currentFrame(): number;
  binInsert?: NleBinInsertChannel;
  authoringFingerprint?: string;
  onIntent: Dispatch;
}) {
  const text = nleCopy(locale).assets;
  const projected = useMemo(
    () => mediaBinProjection(snapshot.assets),
    [snapshot.assets],
  );
  const manifest = useMemo(
    () => buildPublicAssetManifest(snapshot),
    [snapshot],
  );
  const fingerprints = useMemo(
    () =>
      new Map(
        manifest.assets.map((asset) => [
          asset.assetId,
          canonicalPublicRuntimeAssetFingerprint(asset),
        ]),
      ),
    [manifest],
  );
  const root = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState({
    width: 240,
    height: 400,
    scrollTop: 0,
  });
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<MediaFilter>("all");
  const [sort, setSort] = useState<MediaSort>("ordinal_asc");
  const [view, setView] = useState<"grid" | "list">("grid");
  const [cacheRevision, setCacheRevision] = useState(0);
  const [addNotice, setAddNotice] = useState<string | null>(null);
  const pendingAdd = useRef<Readonly<{
    assetId: string;
    clipId: string;
    startFrame: number;
    durationFrames: number;
    timelineRevision: number;
  }> | null>(null);
  const ownedCache = useMemo(() => createNleDecorationCache(), []);
  const cache = decorationCache ?? ownedCache;
  const ownedScheduler = useMemo(
    () => createNleLeaseScheduler<AuthoringMediaAssetDecoration["value"]>(),
    [],
  );
  const scheduler = leaseScheduler ?? ownedScheduler;
  const priorAuthority = useRef({
    workspaceHandle: snapshot.workspaceHandle,
    runtimeEpoch,
  });
  const highlighted = useMemo(
    () => new Set(highlightedAssetIds),
    [highlightedAssetIds],
  );
  const addedAssetIds = useMemo(
    () =>
      new Set([
        ...snapshot.clips.flatMap((clip) =>
          clip.assetId === null ? [] : [clip.assetId],
        ),
      ]),
    [snapshot.clips],
  );
  const selectedProjection = useMemo(
    () =>
      selectMediaBinProjection(projected, {
        query,
        filter,
        sort,
        addedAssetIds,
        cardTemplate: text.card,
      }),
    [projected, query, filter, sort, addedAssetIds, text.card],
  );
  const changeView = (next: "grid" | "list") => {
    root.current?.scrollTo({ top: 0 });
    setViewport((current) => ({ ...current, scrollTop: 0 }));
    setView(next);
  };
  const resetScroll = () => {
    root.current?.scrollTo({ top: 0 });
    setViewport((current) => ({ ...current, scrollTop: 0 }));
  };
  const [menuTarget, setMenuTarget] = useState<TimelineMenuTarget | null>(null);
  const range = virtualMediaRange({
    itemCount: selectedProjection.length,
    viewportWidth: view === "list" ? viewport.width : viewport.width + CARD_GAP,
    viewportHeight: viewport.height,
    scrollTop: viewport.scrollTop,
    minimumCardWidth:
      view === "list" ? Math.max(1, viewport.width) : CARD_PITCH,
    rowHeight: CARD_ROW_HEIGHT,
  });
  const visible = selectedProjection.slice(range.startIndex, range.endIndex);
  const cardDrag = useNleBinCardDrag({
    channel: binInsert,
    busy,
    authorityKey: [
      snapshot.workspaceHandle,
      snapshot.workspaceRevision,
      snapshot.timelineRevision,
      snapshot.timelineFingerprint,
      authoringFingerprint,
      runtimeEpoch,
    ].join("\u0000"),
    visibleAssetIds: visible.map(({ asset }) => asset.assetId),
    authorityFor(assetId) {
      const asset = snapshot.assets.find(
        (candidate) => candidate.assetId === assetId,
      );
      if (asset === undefined) return null;
      const durationFrames = mediaInsertionDurationFrames(
        asset,
        timelineFrameRate(snapshot),
      );
      if (
        durationFrames === null ||
        !Number.isSafeInteger(durationFrames) ||
        durationFrames < 1
      )
        return null;
      return {
        workspaceHandle: snapshot.workspaceHandle,
        workspaceRevision: snapshot.workspaceRevision,
        timelineRevision: snapshot.timelineRevision,
        timelineFingerprint: snapshot.timelineFingerprint,
        ...(authoringFingerprint === undefined ? {} : { authoringFingerprint }),
        assetId,
        durationFrames,
      };
    },
  });

  useEffect(() => {
    const pending = pendingAdd.current;
    if (
      pending === null ||
      snapshot.timelineRevision === pending.timelineRevision
    )
      return;
    pendingAdd.current = null;
    const accepted = snapshot.clips.some(
      (clip) =>
        clip.clipId === pending.clipId &&
        clip.assetId === pending.assetId &&
        clip.startFrame === pending.startFrame &&
        clip.durationFrames === pending.durationFrames,
    );
    setAddNotice(
      primaryAppendNotice(locale, accepted ? "accepted" : "stale_revision"),
    );
  }, [locale, snapshot.clips, snapshot.timelineRevision]);

  useLayoutEffect(() => {
    const element = root.current;
    if (element === null) return;
    const measure = () =>
      setViewport((current) => ({
        ...current,
        width: element.clientWidth || current.width,
        height: element.clientHeight || current.height,
      }));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const workspaceChanged =
      priorAuthority.current.workspaceHandle !== snapshot.workspaceHandle;
    const ownerChanged = priorAuthority.current.runtimeEpoch !== runtimeEpoch;
    if (workspaceChanged)
      cache.purgeWorkspace(priorAuthority.current.workspaceHandle);
    else if (ownerChanged) cache.purgeWorkspace(snapshot.workspaceHandle);
    cache.retainWorkspaceAssets(
      snapshot.workspaceHandle,
      new Set(fingerprints.values()),
    );
    priorAuthority.current = {
      workspaceHandle: snapshot.workspaceHandle,
      runtimeEpoch,
    };
    if (workspaceChanged || ownerChanged)
      setCacheRevision((current) => current + 1);
  }, [cache, fingerprints, runtimeEpoch, snapshot.workspaceHandle]);

  useLayoutEffect(
    () => () => {
      if (leaseScheduler === undefined) ownedScheduler.close();
      if (decorationCache === undefined) ownedCache.close();
    },
    [decorationCache, leaseScheduler, ownedCache, ownedScheduler],
  );

  const publish = useCallback(
    (value: AuthoringMediaAssetDecoration["value"]) => {
      if (value.derivativeKind !== "thumbnail") {
        if (value.derivativeKind === "audio_peaks")
          disposeAuthoringAudioPeaks(value.peaks);
        else value.bitmap.close();
        return;
      }
      cache.set(value.cacheKey, value.bitmap);
      setCacheRevision((current) => current + 1);
    },
    [cache],
  );
  const brokerSource = useMemo(
    () =>
      decorationDemandBroker?.register(
        "nle-media-bin-thumbnails",
        "thumbnail",
        publish,
      ),
    [decorationDemandBroker, publish],
  );
  useEffect(() => () => brokerSource?.close(), [brokerSource]);

  useEffect(() => {
    const client =
      "acquireAssetDecoration" in leaseClient
        ? (leaseClient as AuthoringMediaDecorationLeaseClient)
        : null;
    const demands =
      client === null
        ? []
        : visible.flatMap(({ asset, ordinal }) => {
            const assetFingerprint = fingerprints.get(asset.assetId);
            if (assetFingerprint === undefined) return [];
            const cacheKey = {
              workspaceHandle: snapshot.workspaceHandle,
              assetFingerprint,
              derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
              derivativeKind: "thumbnail",
            } as const;
            if (cache.get(cacheKey) !== undefined) return [];
            return [
              {
                key: `${snapshot.workspaceHandle}:${assetFingerprint}`,
                acquire: (signal: AbortSignal) =>
                  client.acquireAssetDecoration(
                    {
                      snapshot,
                      manifest,
                      assetId: asset.assetId,
                      ownerId: `nle-decoration-${runtimeEpoch}-${ordinal}`,
                      runtimeEpoch,
                      derivativeKind: "thumbnail",
                    },
                    signal,
                  ),
              },
            ];
          });
    if (brokerSource !== undefined) {
      brokerSource.update(demands);
      // IMPORTANT: the workspace broker is shared with timeline filmstrips and waveforms.
      // A timeline snapshot refresh reruns this effect; withdrawing thumbnails in cleanup
      // would churn the shared route set and abort another producer's in-flight derivative.
      return;
    }
    scheduler.updateDecorationDemand(demands, publish);
    return () => scheduler.updateDecorationDemand([], publish);
  }, [
    cache,
    // IMPORTANT: publishing a cached thumbnail must withdraw its completed demand; otherwise
    // another producer's queue refresh repeats the same create even though the bitmap is ready.
    cacheRevision,
    fingerprints,
    leaseClient,
    manifest,
    brokerSource,
    publish,
    runtimeEpoch,
    scheduler,
    snapshot,
    visible.map(({ ordinal }) => ordinal).join(","),
  ]);

  const dispatch = (
    asset: PublicCompositionAsset,
    kind: "asset" | "insert" | "overwrite",
  ) => {
    if (busy) return;
    if (kind === "asset" && pendingAdd.current !== null) {
      setAddNotice(primaryAppendNotice(locale, "pending"));
      return;
    }
    // IMPORTANT: primary append admits video only; images use their image-track insertion
    // path below. Running images through this guard silently refuses every V1 image Add.
    const append =
      kind === "asset" && asset.kind === "video"
        ? admitPrimaryAppend(snapshot, asset)
        : null;
    if (append !== null && !append.admitted) {
      setAddNotice(primaryAppendNotice(locale, append.reason));
      return;
    }
    const draft =
      append?.admitted === true
        ? append.draft
        : insertionDraft(
            snapshot,
            asset,
            currentFrame(),
            kind === "insert" ? "insert_range" : "overwrite_range",
          );
    if (draft === null) return;
    const command =
      kind === "asset"
        ? build.insertAssetClip(draft)
        : kind === "insert"
          ? build.insertRange(draft, [draft.trackId])
          : build.overwriteRange(
              draft,
              [draft.trackId],
              overwriteRemainderIds(snapshot, draft),
            );
    if (kind === "asset") {
      pendingAdd.current = {
        assetId: asset.assetId,
        clipId: draft.clipId,
        startFrame: draft.startFrame,
        durationFrames: draft.durationFrames,
        timelineRevision: snapshot.timelineRevision,
      };
      setAddNotice(primaryAppendNotice(locale, "pending"));
    }
    void onIntent({
      action: "apply_timeline_commands",
      commands: [command],
    }).catch(() => {
      if (pendingAdd.current?.assetId !== asset.assetId) return;
      pendingAdd.current = null;
      setAddNotice(primaryAppendNotice(locale, "stale_revision"));
    });
  };

  const filterLabel: Readonly<Record<MediaFilter, string>> = {
    all: text.filterAll,
    video: text.filterVideo,
    image: text.filterImage,
    added: text.filterAdded,
  };
  const openAssetMenu = (
    element: HTMLElement,
    assetId: string,
    x: number,
    y: number,
  ) => setMenuTarget({ id: assetId, returnFocus: element, x, y });

  return (
    <section className="h3-nle-media-bin" data-h3-nle-region="asset-bin">
      <div className="h3-nle-media-controls">
        {/* M25-63: one toolbar row -- Import, search, sort and filter, and the view pair (D-8). */}
        <div className="h3-nle-media-toolbar">
          {lead}
          <label className="h3-nle-media-search">
            <NleActionIcon name="search" size={14} />
            <input
              type="search"
              data-h3-nle-control="media.search"
              aria-label={text.search}
              placeholder={text.search}
              value={query}
              onChange={(event) => {
                resetScroll();
                setQuery(event.currentTarget.value);
              }}
            />
          </label>
          <NleMediaSortFilterMenu
            locale={locale}
            sort={sort}
            filter={filter}
            onSort={(next) => {
              resetScroll();
              setSort(next);
            }}
            onFilter={(next) => {
              resetScroll();
              setFilter(next);
            }}
          />
          <span
            className="h3-nle-media-views"
            role="group"
            aria-label={text.view}
          >
            <button
              type="button"
              className="h3-nle-bin-icon"
              data-h3-plain
              data-h3-nle-control="media.view.grid"
              aria-label={text.gridView}
              title={text.gridView}
              aria-pressed={view === "grid"}
              onClick={() => changeView("grid")}
            >
              <NleActionIcon name="grid" />
            </button>
            <button
              type="button"
              className="h3-nle-bin-icon"
              data-h3-plain
              data-h3-nle-control="media.view.list"
              aria-label={text.listView}
              title={text.listView}
              aria-pressed={view === "list"}
              onClick={() => changeView("list")}
            >
              <NleActionIcon name="list" />
            </button>
          </span>
        </div>
        <p
          className="h3-nle-media-count"
          data-h3-nle-media-count={selectedProjection.length}
        >
          {fill(text.countLine, {
            filter: filterLabel[filter],
            count: selectedProjection.length,
          })}
        </p>
      </div>
      <div
        ref={root}
        className="h3-nle-media-scroll"
        onScroll={(event) =>
          setViewport((current) => ({
            ...current,
            scrollTop: event.currentTarget.scrollTop,
          }))
        }
      >
        {selectedProjection.length === 0 ? (
          <p className="h3-nle-note">
            {projected.length === 0 ? text.empty : text.noMatches}
          </p>
        ) : null}
        <div aria-hidden="true" style={{ height: range.topSpacerPx }} />
        <ul
          className={`h3-nle-media-grid ${view === "list" ? "h3-nle-media-list" : ""}`}
          data-h3-nle-media-view={view}
          style={{
            gridTemplateColumns: `repeat(${range.columns}, minmax(0, 1fr))`,
          }}
        >
          {visible.map(({ asset, ordinal }) => {
            const ordinalText = String(ordinal).padStart(2, "0");
            const label = fill(text.card, { ordinal: ordinalText });
            const duration = mediaDurationTimecode(
              asset,
              timelineFrameRate(snapshot),
            );
            const added = highlighted.has(asset.assetId)
              ? text.imported
              : snapshot.clips.some((clip) => clip.assetId === asset.assetId)
                ? text.added
                : null;
            const addedPill = highlighted.has(asset.assetId)
              ? text.pillImported
              : text.pillAdded;
            const durationPill = mediaDurationPill(
              asset,
              timelineFrameRate(snapshot),
            );
            const assetFingerprint = fingerprints.get(asset.assetId);
            const bitmap =
              assetFingerprint === undefined
                ? undefined
                : cache.get({
                    workspaceHandle: snapshot.workspaceHandle,
                    assetFingerprint,
                    derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
                    derivativeKind: "thumbnail",
                  });
            const actionLabel = [
              label,
              duration === null
                ? text.durationUnavailable
                : fill(text.duration, { timecode: duration }),
              added,
            ]
              .filter((value): value is string => value !== null)
              .join(". ");
            return (
              <li
                key={asset.assetId}
                data-h3-nle-asset={asset.assetId}
                data-h3-nle-card-index={ordinal}
              >
                <button
                  type="button"
                  className="h3-nle-media-primary"
                  data-h3-plain
                  aria-label={actionLabel}
                  disabled={busy}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    openAssetMenu(
                      event.currentTarget,
                      asset.assetId,
                      event.clientX,
                      event.clientY,
                    );
                  }}
                  {...cardDrag.handlers(asset.assetId)}
                  onClick={(event) => {
                    if (!cardDrag.consumeClick(event)) dispatch(asset, "asset");
                  }}
                  onKeyDown={(event) => {
                    // M25-63: the card's commands menu (APG: Shift+F10 and the ContextMenu key).
                    if (
                      event.key === "ContextMenu" ||
                      (event.shiftKey && event.key === "F10")
                    ) {
                      event.preventDefault();
                      event.stopPropagation();
                      const rect = event.currentTarget.getBoundingClientRect();
                      openAssetMenu(
                        event.currentTarget,
                        asset.assetId,
                        rect.left,
                        rect.bottom,
                      );
                      return;
                    }
                    if (event.key === "Enter") {
                      // Prevent the native synthetic click so one key press owns one transaction.
                      event.preventDefault();
                      dispatch(asset, "asset");
                      return;
                    }
                    if (event.key === "Escape" && cardDrag.escape()) {
                      event.preventDefault();
                      // IMPORTANT: cancelling an owned card drag must consume modal Escape too.
                      // preventDefault alone still lets the workspace's bubble handler close it.
                      event.stopPropagation();
                    }
                  }}
                >
                  <span className="h3-nle-media-art">
                    <BitmapCanvas bitmap={bitmap} />
                    {added === null ? null : (
                      <span
                        className="h3-nle-media-art-badge h3-nle-media-art-added"
                        data-h3-nle-media-badge="added"
                        title={added}
                      >
                        {addedPill}
                      </span>
                    )}
                    <span
                      className="h3-nle-media-art-badge h3-nle-media-art-duration"
                      data-h3-nle-media-badge="duration"
                      title={duration ?? text.durationUnavailable}
                    >
                      {durationPill ?? text.durationUnavailable}
                    </span>
                  </span>
                  <strong>{label}</strong>
                  <span className="h3-nle-thumbnail-status">
                    {bitmap === undefined
                      ? "acquireAssetDecoration" in leaseClient
                        ? text.thumbnailLoading
                        : text.thumbnailUnavailable
                      : ""}
                  </span>
                </button>
                {/* M25-63: "+" over the thumbnail, shown on hover, on focus within the card and
                    always under a coarse pointer. Its tooltip states today's Add (D-6). */}
                <button
                  type="button"
                  className="h3-nle-media-add"
                  data-h3-plain
                  data-h3-nle-control="asset.insert"
                  aria-label={fill(text.addNamed, { name: label })}
                  title={text.addTip}
                  disabled={busy}
                  onClick={() => dispatch(asset, "asset")}
                >
                  <NleActionIcon name="add" size={14} />
                </button>
                {/* The same menu for a coarse pointer, which has no right-click: an inline
                    trigger, as on timeline clips. A fine pointer opens it by right-click,
                    Shift+F10 or the ContextMenu key on the card, so the trigger is not shown. */}
                <button
                  type="button"
                  className="h3-nle-media-more"
                  data-h3-plain
                  data-h3-nle-menu-trigger="asset"
                  aria-haspopup="menu"
                  aria-label={fill(text.moreNamed, { name: label })}
                  title={text.moreActions}
                  disabled={busy}
                  onClick={(event) => {
                    const rect = event.currentTarget.getBoundingClientRect();
                    openAssetMenu(
                      event.currentTarget,
                      asset.assetId,
                      rect.left,
                      rect.bottom,
                    );
                  }}
                >
                  <NleActionIcon name="more" size={14} />
                </button>
              </li>
            );
          })}
        </ul>
        <div aria-hidden="true" style={{ height: range.bottomSpacerPx }} />
        <span hidden data-h3-nle-cache-revision={cacheRevision} />
      </div>
      <NleAssetMenu
        locale={locale}
        target={menuTarget}
        busy={busy}
        onChoose={(assetId, command) => {
          const entry = projected.find(
            ({ asset }) => asset.assetId === assetId,
          );
          if (entry !== undefined) dispatch(entry.asset, command);
        }}
        onClose={() => setMenuTarget(null)}
      />
      {addNotice === null ? null : (
        <p role="status" data-h3-nle-add-notice="">
          {addNotice}
        </p>
      )}
    </section>
  );
}
