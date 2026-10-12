import type { Locale } from "../i18n/catalog";
import type { PerceptionHostBinding } from "../state/perceptionHostState";
import { useEffect } from "react";

const copy = {
  en: {
    title: "Local perception",
    refresh: "Refresh status",
    visual: "Visual analysis",
    audio: "Audio analysis",
    ready: "Ready",
    unavailable: "Unavailable",
    configured: "Configured",
    missing: "Not configured",
    refused: "Configuration refused",
    failed: "Status unavailable",
    loading: "Reading status…",
    cancellation: "Host cancellation is not observed.",
    remedy:
      "Ask the host operator to configure local perception and restart ComfyUI.",
    explanation:
      "Local analysis uses the host operator's existing setup. This page only reads its status.",
  },
  "zh-TW": {
    title: "本機感知",
    refresh: "重新整理狀態",
    visual: "視覺分析",
    audio: "音訊分析",
    ready: "可用",
    unavailable: "不可用",
    configured: "已設定",
    missing: "尚未設定",
    refused: "設定遭拒",
    failed: "無法取得狀態",
    loading: "正在讀取狀態…",
    cancellation: "目前未觀察主機的取消狀態。",
    remedy: "請主機管理者設定本機感知，並重新啟動 ComfyUI。",
    explanation: "本機分析使用主機管理者既有的設定。此頁面僅讀取其狀態。",
  },
  "zh-CN": {
    title: "本地感知",
    refresh: "刷新状态",
    visual: "视觉分析",
    audio: "音频分析",
    ready: "可用",
    unavailable: "不可用",
    configured: "已配置",
    missing: "尚未配置",
    refused: "配置被拒绝",
    failed: "无法获取状态",
    loading: "正在读取状态…",
    cancellation: "当前未观察主机的取消状态。",
    remedy: "请主机管理员配置本地感知，并重新启动 ComfyUI。",
    explanation: "本地分析使用主机管理员现有的配置。此页面仅读取其状态。",
  },
} as const;

export function PerceptionHostStatus({
  locale,
  binding,
}: {
  locale: Locale;
  binding: PerceptionHostBinding;
}) {
  const { ensure, leave, refresh, state } = binding;
  useEffect(() => {
    ensure();
    return leave;
  }, [ensure, leave]);
  const text = copy[locale];
  const status = state.error === null ? state.status : null;
  const disposition = status?.state ?? (state.busy ? "loading" : "unavailable");
  const summary =
    status?.state === "configured"
      ? text.configured
      : status?.state === "not_configured"
        ? text.missing
        : status?.state === "refused"
          ? text.refused
          : state.busy
            ? text.loading
            : text.failed;
  return (
    <section
      className="h3s-pv"
      aria-labelledby="h3-perception-title"
      data-perception-state={disposition}
      aria-busy={state.busy}
    >
      <h3 id="h3-perception-title" className="h3ta">
        {text.title}
      </h3>
      <p className="h3ds">{text.explanation}</p>
      <p role="status" aria-live="polite">
        {summary}
      </p>
      <div className="h3s-r">
        <span>{text.visual}</span>
        <span role="status" aria-label={text.visual}>
          {status?.visual ? text.ready : text.unavailable}
        </span>
      </div>
      <div className="h3s-r">
        <span>{text.audio}</span>
        <span role="status" aria-label={text.audio}>
          {status?.audio ? text.ready : text.unavailable}
        </span>
      </div>
      {status?.state === "configured" && !status.cancellation_observed ? (
        <p>{text.cancellation}</p>
      ) : null}
      {status !== null && status.state !== "configured" ? (
        <p>{text.remedy}</p>
      ) : null}
      <button
        type="button"
        className="h3bt"
        disabled={state.busy}
        onClick={() => {
          void refresh();
        }}
      >
        {text.refresh}
      </button>
    </section>
  );
}
