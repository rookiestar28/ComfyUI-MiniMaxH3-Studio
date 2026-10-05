import type { Locale } from "../i18n/catalog";
import type { OutputPhase } from "../contracts/authoringOutputCodec";
type Copy = {
  title: string;
  render: string;
  cancel: string;
  download: string;
  preview: string;
  player: string;
  close: string;
  old: string;
  empty: string;
  error: string;
  refresh: string;
  loading: string;
  previewError: string;
  gone: string;
  audio: string;
  silent: string;
  /** The extension-owned preview transport; the player exposes no UA control surface. */
  playerControls: string;
  playerPlay: string;
  playerPause: string;
  playerSeek: string;
  playerTime: string;
  phases: Record<OutputPhase, string>;
};
export const outputCopy: Record<Locale, Copy> = {
  en: {
    title: "Final video",
    render: "Render final video",
    cancel: "Cancel render",
    download: "Download original",
    preview: "Preview output",
    player: "Final video preview",
    close: "Close preview",
    old: "Output from an earlier revision",
    empty: "Render the current timeline to create a final video.",
    error:
      "Output status is unavailable. Refresh before accessing this output.",
    refresh: "Refresh status",
    loading: "Loading preview",
    previewError: "Preview unavailable. The original remains separate.",
    gone: "Output is no longer available",
    audio: "Primary-track audio included",
    silent: "No output audio",
    playerControls: "Preview playback controls",
    playerPlay: "Play preview",
    playerPause: "Pause preview",
    playerSeek: "Preview position",
    playerTime: "Second {position} of {duration}",
    phases: {
      queued: "Queued",
      probing: "Inspecting sources",
      preparing: "Preparing",
      rendering: "Rendering",
      encoding: "Encoding",
      muxing: "Assembling video",
      validating: "Verifying output",
      succeeded: "Video ready",
      failed: "Render failed",
      cancelled: "Render cancelled",
    },
  },
  "zh-TW": {
    title: "最終影片",
    render: "算繪最終影片",
    cancel: "取消算繪",
    download: "下載原檔",
    preview: "預覽輸出",
    player: "最終影片預覽",
    close: "關閉預覽",
    old: "此輸出來自較早版本",
    empty: "算繪目前時間軸以建立最終影片。",
    error: "無法取得輸出狀態。請重新整理後再存取。",
    refresh: "重新整理狀態",
    loading: "正在載入預覽",
    previewError: "預覽不可用；原檔仍為獨立輸出。",
    gone: "輸出已不可用",
    audio: "包含主軌音訊",
    silent: "輸出無音訊",
    playerControls: "預覽播放控制項",
    playerPlay: "播放預覽",
    playerPause: "暫停預覽",
    playerSeek: "預覽位置",
    playerTime: "第 {position} 秒，共 {duration} 秒",
    phases: {
      queued: "排隊中",
      probing: "正在檢查來源",
      preparing: "準備中",
      rendering: "算繪中",
      encoding: "編碼中",
      muxing: "正在組合影片",
      validating: "正在驗證輸出",
      succeeded: "影片已就緒",
      failed: "算繪失敗",
      cancelled: "算繪已取消",
    },
  },
  "zh-CN": {
    title: "最终视频",
    render: "渲染最终视频",
    cancel: "取消渲染",
    download: "下载原文件",
    preview: "预览输出",
    player: "最终视频预览",
    close: "关闭预览",
    old: "此输出来自较早版本",
    empty: "渲染当前时间线以创建最终视频。",
    error: "无法获取输出状态。请刷新后再访问。",
    refresh: "刷新状态",
    loading: "正在加载预览",
    previewError: "预览不可用；原文件仍为独立输出。",
    gone: "输出已不可用",
    audio: "包含主轨音频",
    silent: "输出无音频",
    playerControls: "预览播放控件",
    playerPlay: "播放预览",
    playerPause: "暂停预览",
    playerSeek: "预览位置",
    playerTime: "第 {position} 秒，共 {duration} 秒",
    phases: {
      queued: "排队中",
      probing: "正在检查来源",
      preparing: "准备中",
      rendering: "渲染中",
      encoding: "编码中",
      muxing: "正在组合视频",
      validating: "正在验证输出",
      succeeded: "视频已就绪",
      failed: "渲染失败",
      cancelled: "渲染已取消",
    },
  },
};
