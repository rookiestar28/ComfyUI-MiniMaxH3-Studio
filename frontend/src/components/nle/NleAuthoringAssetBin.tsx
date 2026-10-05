import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
  canonicalPublicRuntimeAssetFingerprint,
} from "../../contracts/authoringMediaLeaseCodec";
import type { NleAuthoringStateV2 } from "../../contracts/authoringWorkbenchCodec";
import type { PublicCompositionAsset } from "../../contracts/compositionCodec";
import type {
  AuthoringMediaAssetDecoration,
  AuthoringMediaDecorationLeaseClient,
  AuthoringMediaSourceLeaseClient,
} from "../../host/authoringMediaSourceLease";
import { disposeAuthoringAudioPeaks } from "../../contracts/authoringAudioPeaks";
import type { NleDecorationDemandBroker } from "../../host/nleDecorationDemandBroker";
import { createNleDecorationCache } from "../../host/nleDecorationCache";
import {
  createNleLeaseScheduler,
  type NleLeaseScheduler,
} from "../../host/nleLeaseScheduler";
import type { AuthoringIntent } from "../../state/authoringViewState";
import type { Locale } from "../../i18n/catalog";
import { buildNleAuthoringAssetManifest } from "../../runtime/nleAuthoringAssetManifest";
import { admitTimelineInsert } from "../../runtime/nleTimelineGesture";
import {
  mediaBinProjection,
  mediaDurationTimecode,
  mediaInsertionDurationFrames,
  selectMediaBinProjection,
  virtualMediaRange,
  type MediaFilter,
  type MediaSort,
} from "./nleMediaBinModel";
import {
  NleAssetMenu,
  NleMediaSortFilterMenu,
  type AssetMenuCommand,
} from "./NleBinMenus";
import { NleActionIcon } from "./NleIconActions";
import { fill, nleCopy, primaryAppendNotice } from "./nleCopy";
import {
  build,
  type NewClipDraft,
  type PrimaryAppendDecision,
  type PrimaryAppendRefusal,
} from "./nleCommandBuilders";
import type { TimelineMenuTarget } from "./NleTimelineMenus";
import { useNleBinCardDrag } from "./useNleBinCardDrag";
import type { NleBinInsertChannel } from "../../runtime/nleBinInsertChannel";

const AUTHORING_FRAME_RATE = Object.freeze({ num: 24, den: 1 });
const CARD_PITCH = 148;
const CARD_GAP = 8;
const CARD_ROW_HEIGHT = 128;

function ThumbnailCanvas({ bitmap }: { bitmap: ImageBitmap | undefined }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useLayoutEffect(() => {
    const canvas = ref.current;
    if (canvas === null) return;
    if (bitmap === undefined) {
      canvas.width = 0;
      canvas.height = 0;
      return;
    }
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
  }, [bitmap]);
  return (
    <canvas
      ref={ref}
      aria-hidden="true"
      data-h3-nle-thumbnail=""
      data-h3-nle-canvas="bin_thumbnail"
    />
  );
}

function nextClipId(authoring: NleAuthoringStateV2): string {
  const taken = new Set(authoring.clips.map(({ clipId }) => clipId));
  const base = `clip-r${authoring.timelineRevision}`;
  let index = 1;
  while (taken.has(`${base}-${index}`)) index += 1;
  return `${base}-${index}`;
}

/** The V2 authoring Add path shares the same primary-only, exact-duration append contract. */
export function admitAuthoringPrimaryAppend(
  authoring: NleAuthoringStateV2,
  asset: PublicCompositionAsset,
): PrimaryAppendDecision {
  const primary = authoring.tracks.find(
    (track) => track.kind === "primary_video" && track.enabled,
  );
  if (primary === undefined)
    return Object.freeze({ admitted: false, reason: "primary_missing" });
  if (primary.locked)
    return Object.freeze({ admitted: false, reason: "primary_locked" });
  const sourceDuration = mediaInsertionDurationFrames(
    asset,
    AUTHORING_FRAME_RATE,
  );
  if (sourceDuration === null || sourceDuration < 1)
    return Object.freeze({ admitted: false, reason: "timing_unavailable" });
  const startFrame = authoring.clips
    .filter((clip) => clip.trackId === primary.trackId)
    .reduce(
      (end, clip) => Math.max(end, clip.startFrame + clip.durationFrames),
      0,
    );
  if (startFrame + sourceDuration > authoring.editCapacityFrames)
    return Object.freeze({
      admitted: false,
      reason: "insufficient_capacity",
    });
  const admission = admitTimelineInsert({
    asset,
    startFrame,
    durationFrames: sourceDuration,
    timelineDurationFrames: authoring.editCapacityFrames,
    tracks: authoring.tracks,
    clips: authoring.clips,
    // CRITICAL: primary Add must pin the track already selected above. Letting admission pick
    // by display order can select an earlier video overlay and falsely reject a valid append.
    targetTrackId: primary.trackId,
    operation: "add",
  });
  if (
    authoring.clips.length >= 128 ||
    !admission.admitted ||
    admission.trackId !== primary.trackId
  )
    return Object.freeze({ admitted: false, reason: "append_rejected" });
  return Object.freeze({
    admitted: true,
    draft: Object.freeze({
      clipId: nextClipId(authoring),
      trackId: primary.trackId,
      assetId: asset.assetId,
      startFrame,
      durationFrames: sourceDuration,
      sourceStartFrame: 0,
      text: null,
    }),
  });
}

function assetDraft(
  authoring: NleAuthoringStateV2,
  asset: PublicCompositionAsset,
  startFrame: number,
  operation: "add" | "insert_range" | "overwrite_range" = "add",
): NewClipDraft | null {
  if (
    asset.kind === "font" ||
    authoring.clips.length >= 128 ||
    !Number.isSafeInteger(startFrame) ||
    startFrame < 0 ||
    startFrame >= authoring.editCapacityFrames
  )
    return null;
  const available = authoring.editCapacityFrames - startFrame;
  const sourceDuration = mediaInsertionDurationFrames(
    asset,
    AUTHORING_FRAME_RATE,
  );
  if (available < 1 || sourceDuration === null) return null;
  // IMPORTANT: never hardcode V2 card inserts to primary_video. Image assets require an
  // image_overlay (and range commands need their own overlap rules); bypassing admission turns
  // an otherwise enabled card into a silent no-op or emits an incompatible-track command.
  const admission = admitTimelineInsert({
    asset,
    startFrame,
    durationFrames: Math.min(sourceDuration, available),
    timelineDurationFrames: authoring.editCapacityFrames,
    tracks: authoring.tracks,
    clips: authoring.clips,
    operation,
  });
  if (!admission.admitted || admission.trackId === null) return null;
  return Object.freeze({
    clipId: nextClipId(authoring),
    trackId: admission.trackId,
    assetId: asset.assetId,
    startFrame,
    durationFrames: Math.min(sourceDuration, available),
    sourceStartFrame: 0,
    text: null,
  });
}

function overwriteRemainderIds(
  authoring: NleAuthoringStateV2,
  draft: NewClipDraft,
): Readonly<Record<string, string>> {
  const result: Record<string, string> = {};
  const taken = new Set(authoring.clips.map(({ clipId }) => clipId));
  let index = 1;
  for (const clip of authoring.clips) {
    if (
      clip.trackId !== draft.trackId ||
      clip.startFrame >= draft.startFrame ||
      clip.startFrame + clip.durationFrames <=
        draft.startFrame + draft.durationFrames
    )
      continue;
    let identifier: string;
    do {
      identifier = `remainder-r${authoring.timelineRevision}-${index++}`;
    } while (taken.has(identifier));
    taken.add(identifier);
    result[clip.clipId] = identifier;
  }
  return Object.freeze(result);
}

function dispatchAssetCommand(
  authoring: NleAuthoringStateV2,
  asset: PublicCompositionAsset,
  command: AssetMenuCommand,
  currentFrame: () => number,
  onIntent: (intent: AuthoringIntent) => Promise<void>,
):
  | Readonly<{
      status: "dispatched";
      promise: Promise<void>;
      draft: NewClipDraft;
    }>
  | Readonly<{
      status: "refused";
      reason: PrimaryAppendRefusal;
    }> {
  const operation =
    command === "asset"
      ? "add"
      : command === "insert"
        ? "insert_range"
        : "overwrite_range";
  const append =
    command === "asset" && asset.kind === "video"
      ? admitAuthoringPrimaryAppend(authoring, asset)
      : null;
  if (append !== null && !append.admitted)
    return Object.freeze({ status: "refused", reason: append.reason });
  const startFrame = Math.max(
    0,
    Math.min(authoring.editCapacityFrames - 1, Math.trunc(currentFrame())),
  );
  const draft =
    append?.admitted === true
      ? append.draft
      : assetDraft(authoring, asset, startFrame, operation);
  if (draft === null)
    return Object.freeze({ status: "refused", reason: "append_rejected" });
  const timelineCommand =
    command === "asset"
      ? build.insertAssetClip(draft)
      : command === "insert"
        ? build.insertRange(draft, [draft.trackId])
        : build.overwriteRange(
            draft,
            [draft.trackId],
            overwriteRemainderIds(authoring, draft),
          );
  const promise = onIntent({
    action: "apply_timeline_commands",
    commands: [timelineCommand],
    capturedTimeline: {
      workspaceHandle: authoring.workspaceHandle,
      workspaceRevision: authoring.workspaceRevision,
      timelineRevision: authoring.timelineRevision,
      timelineFingerprint: authoring.timelineFingerprint,
      authoringFingerprint: authoring.authoringFingerprint,
    },
  });
  return Object.freeze({ status: "dispatched", promise, draft });
}

/** V2 builds its asset manifest from the accepted authoring state and uses the V2 asset lease. */
export function NleAuthoringAssetBin({
  locale,
  authoring,
  highlightedAssetIds,
  busy,
  runtimeEpoch,
  leaseClient,
  leaseScheduler,
  decorationCache,
  decorationDemandBroker,
  lead,
  currentFrame,
  binInsert,
  onIntent,
}: {
  locale: Locale;
  authoring: NleAuthoringStateV2;
  highlightedAssetIds: readonly string[];
  busy: boolean;
  runtimeEpoch: number;
  leaseClient: AuthoringMediaSourceLeaseClient;
  leaseScheduler?: NleLeaseScheduler<AuthoringMediaAssetDecoration["value"]>;
  decorationCache?: ReturnType<typeof createNleDecorationCache<ImageBitmap>>;
  decorationDemandBroker?: NleDecorationDemandBroker<
    AuthoringMediaAssetDecoration["value"]
  >;
  lead?: ReactNode;
  binInsert?: NleBinInsertChannel;
  currentFrame(): number;
  onIntent(intent: AuthoringIntent): Promise<void>;
}) {
  const text = nleCopy(locale).assets;
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<MediaFilter>("all");
  const [sort, setSort] = useState<MediaSort>("ordinal_asc");
  const [view, setView] = useState<"grid" | "list">("grid");
  const projected = useMemo(
    () => mediaBinProjection(authoring.assets),
    [authoring.assets],
  );
  const manifest = useMemo(
    () => buildNleAuthoringAssetManifest(authoring),
    [authoring],
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
    workspaceHandle: authoring.workspaceHandle,
    runtimeEpoch,
  });
  const addedAssetIds = useMemo(
    () =>
      new Set(
        authoring.clips.flatMap((clip) =>
          clip.assetId === null ? [] : [clip.assetId],
        ),
      ),
    [authoring.clips],
  );
  const highlighted = useMemo(
    () => new Set(highlightedAssetIds),
    [highlightedAssetIds],
  );
  const selected = useMemo(
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
  const [menuTarget, setMenuTarget] = useState<TimelineMenuTarget | null>(null);
  const range = virtualMediaRange({
    itemCount: selected.length,
    viewportWidth: view === "list" ? viewport.width : viewport.width + CARD_GAP,
    viewportHeight: viewport.height,
    scrollTop: viewport.scrollTop,
    minimumCardWidth:
      view === "list" ? Math.max(1, viewport.width) : CARD_PITCH,
    rowHeight: CARD_ROW_HEIGHT,
  });
  const visible = selected.slice(range.startIndex, range.endIndex);
  const cardDrag = useNleBinCardDrag({
    channel: binInsert,
    busy,
    authorityKey: [
      authoring.workspaceHandle,
      authoring.workspaceRevision,
      authoring.timelineRevision,
      authoring.timelineFingerprint,
      authoring.authoringFingerprint,
      runtimeEpoch,
    ].join("\u0000"),
    visibleAssetIds: visible.map(({ asset }) => asset.assetId),
    authorityFor(assetId) {
      const asset = authoring.assets.find(
        (candidate) => candidate.assetId === assetId,
      );
      if (asset === undefined || asset.kind === "font") return null;
      const durationFrames = mediaInsertionDurationFrames(
        asset,
        AUTHORING_FRAME_RATE,
      );
      if (
        durationFrames === null ||
        !Number.isSafeInteger(durationFrames) ||
        durationFrames < 1
      )
        return null;
      return {
        workspaceHandle: authoring.workspaceHandle,
        workspaceRevision: authoring.workspaceRevision,
        timelineRevision: authoring.timelineRevision,
        timelineFingerprint: authoring.timelineFingerprint,
        authoringFingerprint: authoring.authoringFingerprint,
        assetId,
        durationFrames,
      };
    },
  });
  useEffect(() => {
    const pending = pendingAdd.current;
    if (
      pending === null ||
      authoring.timelineRevision === pending.timelineRevision
    )
      return;
    pendingAdd.current = null;
    const accepted = authoring.clips.some(
      (clip) =>
        clip.clipId === pending.clipId &&
        clip.assetId === pending.assetId &&
        clip.startFrame === pending.startFrame &&
        clip.durationFrames === pending.durationFrames,
    );
    setAddNotice(
      primaryAppendNotice(locale, accepted ? "accepted" : "stale_revision"),
    );
  }, [authoring.clips, authoring.timelineRevision, locale]);
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
      priorAuthority.current.workspaceHandle !== authoring.workspaceHandle;
    const ownerChanged = priorAuthority.current.runtimeEpoch !== runtimeEpoch;
    if (workspaceChanged)
      cache.purgeWorkspace(priorAuthority.current.workspaceHandle);
    else if (ownerChanged) cache.purgeWorkspace(authoring.workspaceHandle);
    cache.retainWorkspaceAssets(
      authoring.workspaceHandle,
      new Set(fingerprints.values()),
    );
    priorAuthority.current = {
      workspaceHandle: authoring.workspaceHandle,
      runtimeEpoch,
    };
    if (workspaceChanged || ownerChanged)
      setCacheRevision((current) => current + 1);
  }, [cache, authoring.workspaceHandle, fingerprints, runtimeEpoch]);
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
        "nle-authoring-asset-bin-thumbnails",
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
              workspaceHandle: authoring.workspaceHandle,
              assetFingerprint,
              derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
              derivativeKind: "thumbnail",
            } as const;
            if (cache.get(cacheKey) !== undefined) return [];
            return [
              {
                key: `${authoring.workspaceHandle}:${assetFingerprint}`,
                kind: "thumbnail",
                acquire: (signal: AbortSignal) =>
                  client.acquireAssetDecoration(
                    {
                      authoring,
                      manifest,
                      assetId: asset.assetId,
                      ownerId: `nle-authoring-decoration-${runtimeEpoch}-${ordinal}`,
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
      return;
    }
    scheduler.updateDecorationDemand(demands, publish);
  }, [
    authoring,
    brokerSource,
    cacheRevision,
    cache,
    fingerprints,
    leaseClient,
    manifest,
    publish,
    runtimeEpoch,
    scheduler,
    visible.map(({ ordinal }) => ordinal).join(","),
  ]);
  useEffect(() => {
    if (brokerSource !== undefined) return;
    return () => scheduler.updateDecorationDemand([], publish);
  }, [brokerSource, publish, scheduler]);
  const filterLabel: Readonly<Record<MediaFilter, string>> = {
    all: text.filterAll,
    video: text.filterVideo,
    image: text.filterImage,
    added: text.filterAdded,
  };
  const dispatch = (
    asset: PublicCompositionAsset,
    command: AssetMenuCommand,
  ) => {
    const isPrimaryAdd = command === "asset" && asset.kind === "video";
    if (isPrimaryAdd && pendingAdd.current !== null) {
      setAddNotice(primaryAppendNotice(locale, "pending"));
      return;
    }
    const result = dispatchAssetCommand(
      authoring,
      asset,
      command,
      currentFrame,
      onIntent,
    );
    if (result.status === "refused") {
      if (command === "asset")
        setAddNotice(primaryAppendNotice(locale, result.reason));
      return;
    }
    if (!isPrimaryAdd) return;
    pendingAdd.current = {
      assetId: asset.assetId,
      clipId: result.draft.clipId,
      startFrame: result.draft.startFrame,
      durationFrames: result.draft.durationFrames,
      timelineRevision: authoring.timelineRevision,
    };
    setAddNotice(primaryAppendNotice(locale, "pending"));
    void result.promise.catch(() => {
      if (pendingAdd.current?.assetId !== asset.assetId) return;
      pendingAdd.current = null;
      setAddNotice(primaryAppendNotice(locale, "stale_revision"));
    });
  };
  return (
    <section className="h3-nle-media-bin" data-h3-nle-region="asset-bin">
      <div className="h3-nle-media-controls">
        <div className="h3-nle-media-toolbar">
          {lead}
          <label className="h3-nle-media-search">
            <input
              type="search"
              data-h3-nle-control="media.search"
              aria-label={text.search}
              placeholder={text.search}
              value={query}
              onChange={(event) => setQuery(event.currentTarget.value)}
            />
          </label>
          <NleMediaSortFilterMenu
            locale={locale}
            sort={sort}
            filter={filter}
            onSort={setSort}
            onFilter={setFilter}
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
              aria-pressed={view === "grid"}
              onClick={() => setView("grid")}
            >
              <NleActionIcon name="grid" />
            </button>
            <button
              type="button"
              className="h3-nle-bin-icon"
              data-h3-plain
              data-h3-nle-control="media.view.list"
              aria-label={text.listView}
              aria-pressed={view === "list"}
              onClick={() => setView("list")}
            >
              <NleActionIcon name="list" />
            </button>
          </span>
        </div>
        <p
          className="h3-nle-media-count"
          data-h3-nle-media-count={selected.length}
        >
          {fill(text.countLine, {
            filter: filterLabel[filter],
            count: selected.length,
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
        {selected.length === 0 ? (
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
            const name = fill(text.card, {
              ordinal: String(ordinal).padStart(2, "0"),
            });
            const duration = mediaDurationTimecode(asset, AUTHORING_FRAME_RATE);
            const added = addedAssetIds.has(asset.assetId);
            const imported = highlighted.has(asset.assetId);
            const assetFingerprint = fingerprints.get(asset.assetId);
            const bitmap =
              assetFingerprint === undefined
                ? undefined
                : cache.get({
                    workspaceHandle: authoring.workspaceHandle,
                    assetFingerprint,
                    derivativeProfileId: AUTHORING_MEDIA_DERIVATIVE_PROFILE_ID,
                    derivativeKind: "thumbnail",
                  });
            const insertable = !busy && asset.kind !== "font";
            return (
              <li key={asset.assetId} data-h3-nle-asset={asset.assetId}>
                <button
                  type="button"
                  className="h3-nle-media-primary"
                  data-h3-plain
                  data-h3-nle-control="asset.insert"
                  aria-label={fill(text.addNamed, { name })}
                  disabled={!insertable}
                  {...cardDrag.handlers(asset.assetId)}
                  onClick={(event) => {
                    if (!cardDrag.consumeClick(event)) dispatch(asset, "asset");
                  }}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    setMenuTarget({
                      id: asset.assetId,
                      returnFocus: event.currentTarget,
                      x: event.clientX,
                      y: event.clientY,
                    });
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Escape" && cardDrag.escape()) {
                      event.preventDefault();
                      // IMPORTANT: the modal handles bubbling Escape even after preventDefault.
                      // The owned card drag must cancel before that event can close its workspace.
                      event.stopPropagation();
                      return;
                    }
                    if (
                      event.key !== "ContextMenu" &&
                      !(event.shiftKey && event.key === "F10")
                    )
                      return;
                    event.preventDefault();
                    event.stopPropagation();
                    const rect = event.currentTarget.getBoundingClientRect();
                    setMenuTarget({
                      id: asset.assetId,
                      returnFocus: event.currentTarget,
                      x: rect.left,
                      y: rect.bottom,
                    });
                  }}
                >
                  <span className="h3-nle-media-art">
                    <ThumbnailCanvas bitmap={bitmap} />
                    {text.kind[asset.kind]}
                    {imported ? (
                      <span
                        className="h3-nle-media-art-badge h3-nle-media-art-added"
                        data-h3-nle-media-badge="added"
                      >
                        {text.pillImported}
                      </span>
                    ) : added ? (
                      <span
                        className="h3-nle-media-art-badge h3-nle-media-art-added"
                        data-h3-nle-media-badge="added"
                      >
                        {text.pillAdded}
                      </span>
                    ) : null}
                  </span>
                  <strong>{name}</strong>
                  <span className="h3-nle-thumbnail-status">
                    {bitmap === undefined
                      ? text.thumbnailLoading
                      : duration === null
                        ? text.durationUnavailable
                        : fill(text.duration, { timecode: duration })}
                  </span>
                </button>
                <button
                  type="button"
                  className="h3-nle-media-more"
                  data-h3-plain
                  data-h3-nle-menu-trigger="asset"
                  aria-haspopup="menu"
                  aria-label={fill(text.moreNamed, { name })}
                  title={text.moreActions}
                  disabled={busy}
                  onClick={(event) => {
                    const rect = event.currentTarget.getBoundingClientRect();
                    setMenuTarget({
                      id: asset.assetId,
                      returnFocus: event.currentTarget,
                      x: rect.left,
                      y: rect.bottom,
                    });
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
      {/* IMPORTANT: V2 cards must retain the accepted card menu. Omitting it makes range edits
          unreachable whenever the strict V2 authoring projection is active. */}
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
