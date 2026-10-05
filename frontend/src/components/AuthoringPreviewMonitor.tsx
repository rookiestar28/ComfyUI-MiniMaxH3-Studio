import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";

import {
  createAuthoringFrameCoordinator,
  previewSecondsForTimelineFrame,
  type AuthoringPreviewIdentity,
  type AuthoringPreviewOpener,
} from "../host/authoringFrameCoordinator";
import type { AuthoringAdjacentRelation } from "../host/authoringAdjacentPreview";
import type { Locale } from "../i18n/catalog";
import {
  intentIdentity,
  type MediaRuntimeIntent,
  type MediaToolsBinding,
} from "../state/mediaRuntimeState";
import {
  MediaToolsCard,
  mediaToolsCardVisible,
  type MediaToolsPlacement,
} from "./MediaToolsCard";
import { plainReason } from "./plainReasons";

const copy = {
  en: {
    title: "Selected clip preview",
    preview: "Preview {id}",
    play: "Play preview",
    pause: "Pause preview",
    close: "Close preview",
    loading: "Loading preview",
    ready: "Ready at frame {frame}",
    paused: "Paused at frame {frame}",
    playing: "Playing at frame {frame}",
    seeking: "Seeking at frame {frame}",
    failed: "Preview failed",
    outside: "Frame {frame} is outside selected clip {id}",
    continuity: {
      warming: "Preparing the contiguous next clip",
      warm_ready: "Contiguous next clip ready",
      single_clip_fallback: "Adjacent preview unavailable; using one clip",
      gap: "Timeline gap after this clip",
      overlap: "Timeline overlap is not composed",
      single_clip: "No contiguous next clip",
    },
    embeddedAudio: {
      present_bound: "Embedded audio follows this preview only",
      absent: "This preview has no embedded audio",
      unavailable: "Embedded audio status is unavailable",
    },
    announcement: {
      idle: "Preview ready",
      loading: "Preview loading",
      ready: "Preview ready",
      paused: "Preview paused",
      playing: "Preview playing",
      seeking: "Preview seeking",
      unavailable: "Preview unavailable",
      failed: "Preview failed",
      outside: "Preview paused outside the selected clip",
    },
  },
  "zh-TW": {
    title: "已選剪輯預覽",
    preview: "預覽 {id}",
    play: "播放預覽",
    pause: "暫停預覽",
    close: "關閉預覽",
    loading: "正在載入預覽",
    ready: "已就緒，影格 {frame}",
    paused: "已暫停，影格 {frame}",
    playing: "播放中，影格 {frame}",
    seeking: "搜尋中，影格 {frame}",
    failed: "預覽失敗",
    outside: "影格 {frame} 位於已選剪輯 {id} 之外",
    continuity: {
      warming: "正在準備相鄰的下一個剪輯",
      warm_ready: "相鄰的下一個剪輯已就緒",
      single_clip_fallback: "相鄰預覽不可用；使用單一剪輯",
      gap: "此剪輯後方有時間軸間隙",
      overlap: "不合成時間軸重疊",
      single_clip: "沒有相鄰的下一個剪輯",
    },
    embeddedAudio: {
      present_bound: "嵌入音訊僅跟隨此預覽",
      absent: "此預覽沒有嵌入音訊",
      unavailable: "無法取得嵌入音訊狀態",
    },
    announcement: {
      idle: "預覽已就緒",
      loading: "預覽載入中",
      ready: "預覽已就緒",
      paused: "預覽已暫停",
      playing: "預覽播放中",
      seeking: "預覽搜尋中",
      unavailable: "預覽不可用",
      failed: "預覽失敗",
      outside: "預覽已暫停於所選剪輯之外",
    },
  },
  "zh-CN": {
    title: "已选剪辑预览",
    preview: "预览 {id}",
    play: "播放预览",
    pause: "暂停预览",
    close: "关闭预览",
    loading: "正在加载预览",
    ready: "已就绪，帧 {frame}",
    paused: "已暂停，帧 {frame}",
    playing: "播放中，帧 {frame}",
    seeking: "定位中，帧 {frame}",
    failed: "预览失败",
    outside: "帧 {frame} 位于已选剪辑 {id} 之外",
    continuity: {
      warming: "正在准备相邻的下一个剪辑",
      warm_ready: "相邻的下一个剪辑已就绪",
      single_clip_fallback: "相邻预览不可用；使用单个剪辑",
      gap: "此剪辑后有时间线间隙",
      overlap: "不合成时间线重叠",
      single_clip: "没有相邻的下一个剪辑",
    },
    embeddedAudio: {
      present_bound: "嵌入音频仅跟随此预览",
      absent: "此预览没有嵌入音频",
      unavailable: "无法获取嵌入音频状态",
    },
    announcement: {
      idle: "预览已就绪",
      loading: "预览加载中",
      ready: "预览已就绪",
      paused: "预览已暂停",
      playing: "预览播放中",
      seeking: "预览定位中",
      unavailable: "预览不可用",
      failed: "预览失败",
      outside: "预览已暂停于所选剪辑之外",
    },
  },
} as const;

function fill(value: string, values: Record<string, string | number>): string {
  let output = value;
  for (const [key, replacement] of Object.entries(values))
    output = output.replace(`{${key}}`, String(replacement));
  return output;
}

type PresentedVideo = HTMLVideoElement & {
  requestVideoFrameCallback?: (
    callback: (now: number, metadata: { mediaTime: number }) => void,
  ) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
};

export function AuthoringPreviewMonitor({
  identity,
  warmIdentity,
  continuityRelation = "none",
  locale,
  openPreview,
  returnFocus,
  onClose,
  requestedFrame,
  outsideSelectedClip = false,
  onFrameObserved,
  mediaTools,
  mediaPlacement = "contextual-sidebar",
  overlayGeneration = null,
}: {
  identity: AuthoringPreviewIdentity;
  warmIdentity?: AuthoringPreviewIdentity;
  continuityRelation?: AuthoringAdjacentRelation;
  locale: Locale;
  openPreview: AuthoringPreviewOpener;
  returnFocus: HTMLElement | null;
  onClose(): void;
  requestedFrame?: number;
  outsideSelectedClip?: boolean;
  onFrameObserved?(frame: number): void;
  /** M25-33: the shared Media tools entry shown when the runtime blocks this preview. */
  mediaTools?: MediaToolsBinding;
  mediaPlacement?: Exclude<MediaToolsPlacement, "settings">;
  /** The expanded editor generation this monitor belongs to; `null` in the sidebar. */
  overlayGeneration?: number | null;
}) {
  const [, changed] = useReducer((value: number) => value + 1, 0);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const warmVideoRef = useRef<HTMLVideoElement | null>(null);
  const transportButtonRef = useRef<HTMLButtonElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreCloseOnFailure = useRef(false);
  const audioArmed = useRef(false);
  const lastObservedFrame = useRef<number | undefined>(undefined);
  // M25-33: bumped once when a media setup continues exactly this preview; it reopens it.
  const [reopened, reopen] = useState(0);
  const coordinator = useMemo(
    () =>
      createAuthoringFrameCoordinator({
        openPreview,
        createObjectURL: (blob) => URL.createObjectURL(blob),
        revokeObjectURL: (url) => URL.revokeObjectURL(url),
        changed,
      }),
    [openPreview],
  );
  const {
    workspaceHandle,
    referenceRevision,
    timelineRevision,
    timelineContentFingerprint,
    clipId,
    startFrame,
    frames,
    sourceStartFrame,
    fps,
  } = identity;

  useEffect(() => {
    lastObservedFrame.current = undefined;
    audioArmed.current = false;
    void coordinator.open(
      {
        workspaceHandle,
        referenceRevision,
        timelineRevision,
        timelineContentFingerprint,
        clipId,
        startFrame,
        frames,
        sourceStartFrame,
        fps,
      },
      warmIdentity,
    );
    return () => {
      const video = videoRef.current;
      const warmVideo = warmVideoRef.current;
      // Clear the native player before URL revocation so no stale decoder keeps the Blob alive.
      if (video !== null) {
        if (!video.paused) video.pause();
        video.removeAttribute("src");
      }
      if (warmVideo !== null) {
        if (!warmVideo.paused) warmVideo.pause();
        warmVideo.removeAttribute("src");
      }
      coordinator.close();
    };
  }, [
    coordinator,
    workspaceHandle,
    referenceRevision,
    timelineRevision,
    timelineContentFingerprint,
    clipId,
    startFrame,
    frames,
    sourceStartFrame,
    fps,
    reopened,
    warmIdentity?.workspaceHandle,
    warmIdentity?.referenceRevision,
    warmIdentity?.timelineRevision,
    warmIdentity?.timelineContentFingerprint,
    warmIdentity?.clipId,
    warmIdentity?.startFrame,
    warmIdentity?.frames,
    warmIdentity?.sourceStartFrame,
    warmIdentity?.fps,
  ]);

  const snapshot = coordinator.snapshot();
  const mediaIntent = useMemo<MediaRuntimeIntent>(
    () => ({
      kind: "clip_preview",
      workspaceHandle,
      referenceRevision,
      timelineRevision,
      timelineContentFingerprint,
      clipId,
      overlayGeneration,
    }),
    [
      workspaceHandle,
      referenceRevision,
      timelineRevision,
      timelineContentFingerprint,
      clipId,
      overlayGeneration,
    ],
  );
  const resume = mediaTools?.state.resume ?? null;
  // IMPORTANT: a continuation is consumed once. A token seen at mount belongs to an earlier
  // preview; only a later token whose identity is this exact clip, revision and overlay reopens.
  const seenResume = useRef(resume?.token ?? 0);
  useEffect(() => {
    if (resume === null || resume.token === seenResume.current) return;
    seenResume.current = resume.token;
    if (
      resume.kind === "clip_preview" &&
      resume.identity === intentIdentity(mediaIntent)
    )
      reopen((value) => value + 1);
  }, [resume, mediaIntent]);
  const runtimeBlocked =
    snapshot.status === "unavailable" && snapshot.reason === "unsupported";
  const activeIdentity =
    warmIdentity !== undefined && snapshot.clipId === warmIdentity.clipId
      ? warmIdentity
      : identity;

  useEffect(() => {
    const video = videoRef.current;
    if (video === null || snapshot.objectUrl === undefined) return;
    if (outsideSelectedClip) {
      // IMPORTANT: a gap retains the explicitly selected player but must freeze it;
      // observing the resulting pause would incorrectly pull the playhead back in-range.
      video.pause();
      return;
    }
    if (requestedFrame === undefined) return;
    if (requestedFrame === lastObservedFrame.current) return;
    const seconds = previewSecondsForTimelineFrame(
      activeIdentity,
      requestedFrame,
    );
    // A presented media time may differ from its canonical frame boundary by up
    // to half a frame. Re-seeking inside that interval creates an observation /
    // controlled-input feedback loop and can starve the owned Play/Pause state.
    if (
      Math.abs(video.currentTime - seconds) <=
      1 / (activeIdentity.fps * 2) + Number.EPSILON
    )
      return;
    video.currentTime = seconds;
    coordinator.observe(seconds, "seeking");
  }, [
    coordinator,
    clipId,
    fps,
    outsideSelectedClip,
    requestedFrame,
    snapshot.objectUrl,
    startFrame,
    activeIdentity,
  ]);

  useEffect(() => {
    if (
      outsideSelectedClip ||
      onFrameObserved === undefined ||
      snapshot.objectUrl === undefined ||
      snapshot.playheadFrame === undefined
    )
      return;
    lastObservedFrame.current = snapshot.playheadFrame;
    onFrameObserved(snapshot.playheadFrame);
  }, [
    onFrameObserved,
    outsideSelectedClip,
    snapshot.objectUrl,
    snapshot.playheadFrame,
  ]);

  useEffect(() => {
    const video = videoRef.current as PresentedVideo | null;
    if (video === null || snapshot.objectUrl === undefined) return;
    const request = video.requestVideoFrameCallback?.bind(video);
    const cancel = video.cancelVideoFrameCallback?.bind(video);
    if (request === undefined || cancel === undefined) return;
    let active = true;
    let handle = 0;
    const observe = (_now: number, metadata: { mediaTime: number }) => {
      if (!active) return;
      if (!outsideSelectedClip)
        coordinator.observe(
          metadata.mediaTime,
          video.seeking ? "seeking" : video.paused ? "paused" : "playing",
        );
      handle = request(observe);
    };
    handle = request(observe);
    return () => {
      active = false;
      cancel(handle);
    };
  }, [coordinator, outsideSelectedClip, snapshot.objectUrl]);

  const text = copy[locale];
  const frame = snapshot.playheadFrame ?? activeIdentity.startFrame;
  const label = outsideSelectedClip
    ? fill(text.outside, {
        frame: requestedFrame ?? frame,
        id: activeIdentity.clipId,
      })
    : snapshot.status === "loading"
      ? text.loading
      : snapshot.status === "unavailable"
        ? plainReason(locale, "previewUnavailable", snapshot.reason)
        : snapshot.status === "failed"
          ? text.failed
          : fill(text[snapshot.status === "idle" ? "ready" : snapshot.status], {
              frame,
            });
  const announcement = outsideSelectedClip
    ? text.announcement.outside
    : text.announcement[snapshot.status];
  useLayoutEffect(() => {
    if (
      restoreCloseOnFailure.current &&
      snapshot.objectUrl === undefined &&
      (snapshot.status === "unavailable" || snapshot.status === "failed")
    ) {
      // IMPORTANT: the transport button is removed on failure; restore the local Close owner
      // synchronously or focus escapes into the host document.
      restoreCloseOnFailure.current = false;
      closeButtonRef.current?.focus();
    }
  }, [snapshot.objectUrl, snapshot.status]);
  const close = () => {
    coordinator.close();
    onClose();
    if (returnFocus?.isConnected) returnFocus.focus();
  };
  const observeFallback = (video: HTMLVideoElement) => {
    if (outsideSelectedClip) return;
    const presented = video as PresentedVideo;
    if (presented.requestVideoFrameCallback !== undefined) return;
    coordinator.observe(
      video.currentTime,
      video.seeking ? "seeking" : video.paused ? "paused" : "playing",
    );
  };
  const togglePlayback = () => {
    const video = videoRef.current;
    if (video === null) return;
    if (snapshot.playbackActive) {
      video.pause();
      return;
    }
    audioArmed.current = true;
    video.muted = snapshot.audioDisposition !== "present_bound";
    // IMPORTANT: playback is one explicit user-owned attempt. A rejection must close the
    // Blob/player instead of retrying, falling back, or leaving a no-op transport control.
    void video.play().catch(() => {
      restoreCloseOnFailure.current = true;
      coordinator.unavailable("play_rejected");
    });
  };
  const continuityState =
    snapshot.warmStatus === "ready"
      ? "warm_ready"
      : snapshot.warmStatus === "loading"
        ? "warming"
        : snapshot.warmStatus === "unavailable" ||
            (continuityRelation === "contiguous" && warmIdentity === undefined)
          ? "single_clip_fallback"
          : continuityRelation === "gap"
            ? "gap"
            : continuityRelation === "overlap"
              ? "overlap"
              : "single_clip";
  const promoteWarm = (video: HTMLVideoElement) => {
    if (snapshot.warmStatus !== "ready" || warmIdentity === undefined)
      return false;
    // IMPORTANT: detach the active decoder before coordinator promotion revokes its URL;
    // reversing this order leaves Chromium holding a revoked source through the boundary.
    if (!video.paused) video.pause();
    video.removeAttribute("src");
    if (!coordinator.promoteWarm()) return false;
    lastObservedFrame.current = warmIdentity.startFrame;
    onFrameObserved?.(warmIdentity.startFrame);
    return true;
  };

  return (
    <section className="h3a-preview" aria-label={text.title}>
      <p
        role={snapshot.status === "failed" ? "alert" : "status"}
        aria-live={snapshot.status === "failed" ? undefined : "off"}
      >
        {label}
      </p>
      {/* IMPORTANT: the visual label includes frames and updates on native media callbacks.
          Keep that live region off and announce only stable transport states to avoid flooding. */}
      {snapshot.status !== "failed" ? (
        <p
          className="h3a-preview-announcement"
          data-h3-preview-announcement="true"
          aria-live="polite"
          aria-atomic="true"
        >
          {announcement}
        </p>
      ) : null}
      {mediaTools !== undefined &&
      mediaToolsCardVisible(mediaTools.state, "preview", runtimeBlocked) ? (
        <MediaToolsCard
          placement={mediaPlacement}
          binding={mediaTools}
          locale={locale}
          feature="preview"
          intent={mediaIntent}
        />
      ) : null}
      <p data-h3-continuity={continuityState}>
        {text.continuity[continuityState]}
      </p>
      {snapshot.audioDisposition !== undefined ? (
        <p
          data-h3-embedded-audio={snapshot.audioDisposition}
          data-h3-preview-only="true"
        >
          {text.embeddedAudio[snapshot.audioDisposition]}
        </p>
      ) : null}
      {snapshot.objectUrl !== undefined ? (
        <video
          ref={videoRef}
          aria-label={fill(text.preview, { id: activeIdentity.clipId })}
          src={snapshot.objectUrl}
          muted={
            snapshot.audioDisposition !== "present_bound" || !audioArmed.current
          }
          playsInline
          preload="metadata"
          onLoadedMetadata={(event) => {
            event.currentTarget.muted =
              snapshot.audioDisposition !== "present_bound" ||
              !audioArmed.current;
            if (snapshot.playbackActive)
              void event.currentTarget
                .play()
                .catch(() => coordinator.unavailable("play_rejected"));
            observeFallback(event.currentTarget);
          }}
          onPlay={(event) => {
            audioArmed.current = true;
            if (!outsideSelectedClip)
              coordinator.observe(event.currentTarget.currentTime, "playing");
          }}
          onPause={(event) => {
            if (!outsideSelectedClip)
              coordinator.observe(event.currentTarget.currentTime, "paused");
          }}
          onSeeking={(event) => {
            if (!outsideSelectedClip)
              coordinator.observe(event.currentTarget.currentTime, "seeking");
          }}
          onSeeked={(event) => observeFallback(event.currentTarget)}
          onTimeUpdate={(event) => observeFallback(event.currentTarget)}
          onEnded={(event) => {
            if (!promoteWarm(event.currentTarget))
              observeFallback(event.currentTarget);
          }}
          onError={() => {
            if (document.activeElement === transportButtonRef.current)
              restoreCloseOnFailure.current = true;
            coordinator.fail();
          }}
          onVolumeChange={(event) => {
            event.currentTarget.muted =
              snapshot.audioDisposition !== "present_bound" ||
              !audioArmed.current;
          }}
        />
      ) : null}
      {snapshot.warmObjectUrl !== undefined ? (
        <video
          ref={warmVideoRef}
          data-h3-preview-role="warm"
          src={snapshot.warmObjectUrl}
          aria-hidden="true"
          tabIndex={-1}
          muted
          playsInline
          preload="auto"
          onLoadedMetadata={(event) => {
            event.currentTarget.muted = true;
          }}
          onVolumeChange={(event) => {
            event.currentTarget.muted = true;
          }}
        />
      ) : null}
      <div className="h3a-preview-controls">
        {snapshot.objectUrl !== undefined && !outsideSelectedClip ? (
          <button
            ref={transportButtonRef}
            type="button"
            onClick={togglePlayback}
          >
            {snapshot.playbackActive ? text.pause : text.play}
          </button>
        ) : null}
        <button ref={closeButtonRef} type="button" onClick={close}>
          {text.close}
        </button>
      </div>
    </section>
  );
}

export const authoringPreviewTriggerLabel = (locale: Locale, clipId: string) =>
  fill(copy[locale].preview, { id: clipId });
