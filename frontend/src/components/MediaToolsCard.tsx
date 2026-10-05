// M25-33: the one Media tools card, in Settings and where import, clip preview or final output is
// blocked by the backend media runtime.
//
// The card renders only the decoded status and the one setup job the session holds. Normal screens
// show availability, the install source, its approximate size, license and release page, and at
// most one primary action; locators, hashes and codec lists never appear. The advanced folder field
// lives in Settings only, behind a disclosure.

import { useEffect, useId, useRef, useState } from "react";

import {
  MEDIA_RUNTIME_FEATURES,
  type MediaRuntimeFeature,
  type MediaRuntimeFeatureReadiness,
  type MediaRuntimeJob,
  type MediaRuntimeStatus,
} from "../contracts/mediaRuntimeCodec";
import type { Locale } from "../i18n/catalog";
import {
  MEDIA_RUNTIME_INTENT_FEATURE,
  type MediaRuntimeIntent,
  type MediaRuntimeState,
  type MediaToolsBinding,
} from "../state/mediaRuntimeState";
import { plainReason } from "./plainReasons";

const copy = {
  en: {
    title: "Media tools",
    description:
      "Import, clip preview and final output use media tools on this ComfyUI host.",
    checking: "Checking media tools…",
    statusUnavailable: "Media tools status is unavailable.",
    checkAgain: "Check again",
    ready: "Media tools are ready.",
    source: {
      managed: "Installed by this extension.",
      explicit_override: "Set up by the host administrator.",
      local_selection: "Using the folder you chose.",
      python_prefix: "Found with this ComfyUI installation.",
      system_path: "Found on this computer.",
    },
    needed: {
      settings: "Import, clip preview and final output need media tools.",
      import: "Importing needs media tools.",
      preview: "Clip preview needs media tools.",
      derivatives: "Clip preparation needs media tools.",
      assembly: "Sequence assembly needs media tools.",
      render: "Final output needs media tools.",
    },
    details: "{source}, about {size} MB, {license}.",
    releasePage: "Release page",
    install: "Install",
    installAndContinue: "Install and continue",
    installAgain: "Install again",
    cancel: "Cancel setup",
    progress: "Setup progress",
    progressBytes: "{done} of {total} MB",
    phase: {
      checking: "Checking the download…",
      downloading: "Downloading media tools…",
      extracting: "Extracting media tools…",
      verifying: "Verifying media tools…",
      publishing: "Installing media tools…",
      activating: "Starting media tools…",
      done: "Finishing setup…",
    },
    failed: "Setup did not finish.",
    adminControl:
      "Change the media tools setting on the ComfyUI host, then check again.",
    starting: "Media tools are starting…",
    renderUnavailable: "Final output is not available on this host.",
    recovery: "An earlier copy of the media tools is kept aside.",
    recoveryKept:
      "It stays in place until the current media tools are confirmed ready.",
    reclaim: "Remove earlier copy",
    advanced: "Advanced",
    folder: "Media tools folder",
    useFolder: "Use this folder",
    restoreAuto: "Use automatic setup",
    dismiss: "Dismiss",
    notice: {
      continued: "Media tools are ready. Continuing.",
      select_again:
        "Media tools are ready, but the selection changed. Select again.",
      continuation_cleared: "The waiting action was cleared.",
      continuation_replaced:
        "The earlier waiting action was replaced by this one.",
      feature_not_ready:
        "Setup finished, but this feature is still unavailable.",
      outcome_unknown: "The setup request could not be confirmed. Check again.",
    },
  },
  "zh-TW": {
    title: "媒體工具",
    description: "匯入、剪輯預覽與最終輸出會使用此 ComfyUI 主機上的媒體工具。",
    checking: "正在檢查媒體工具…",
    statusUnavailable: "無法取得媒體工具狀態。",
    checkAgain: "再次檢查",
    ready: "媒體工具已就緒。",
    source: {
      managed: "由此擴充功能安裝。",
      explicit_override: "由主機管理員設定。",
      local_selection: "使用您選擇的資料夾。",
      python_prefix: "在此 ComfyUI 安裝中找到。",
      system_path: "在此電腦上找到。",
    },
    needed: {
      settings: "匯入、剪輯預覽與最終輸出需要媒體工具。",
      import: "匯入需要媒體工具。",
      preview: "剪輯預覽需要媒體工具。",
      derivatives: "準備剪輯需要媒體工具。",
      assembly: "組裝序列需要媒體工具。",
      render: "最終輸出需要媒體工具。",
    },
    details: "{source}，約 {size} MB，{license}。",
    releasePage: "發行頁面",
    install: "安裝",
    installAndContinue: "安裝並繼續",
    installAgain: "重新安裝",
    cancel: "取消安裝",
    progress: "安裝進度",
    progressBytes: "{done} / {total} MB",
    phase: {
      checking: "正在檢查下載…",
      downloading: "正在下載媒體工具…",
      extracting: "正在解壓縮媒體工具…",
      verifying: "正在驗證媒體工具…",
      publishing: "正在安裝媒體工具…",
      activating: "正在啟動媒體工具…",
      done: "正在完成安裝…",
    },
    failed: "安裝未完成。",
    adminControl: "請在 ComfyUI 主機上變更媒體工具設定，然後再次檢查。",
    starting: "媒體工具正在啟動…",
    renderUnavailable: "此主機無法提供最終輸出。",
    recovery: "較早的媒體工具副本已另外保留。",
    recoveryKept: "在確認目前的媒體工具就緒之前，會保留這份副本。",
    reclaim: "移除較早的副本",
    advanced: "進階",
    folder: "媒體工具資料夾",
    useFolder: "使用此資料夾",
    restoreAuto: "使用自動設定",
    dismiss: "關閉",
    notice: {
      continued: "媒體工具已就緒，正在繼續。",
      select_again: "媒體工具已就緒，但選取內容已變更，請重新選取。",
      continuation_cleared: "已清除等待中的動作。",
      continuation_replaced: "較早等待中的動作已由此動作取代。",
      feature_not_ready: "安裝已完成，但此功能仍無法使用。",
      outcome_unknown: "無法確認安裝要求的結果，請再次檢查。",
    },
  },
  "zh-CN": {
    title: "媒体工具",
    description: "导入、剪辑预览与最终输出会使用此 ComfyUI 主机上的媒体工具。",
    checking: "正在检查媒体工具…",
    statusUnavailable: "无法获取媒体工具状态。",
    checkAgain: "再次检查",
    ready: "媒体工具已就绪。",
    source: {
      managed: "由此扩展安装。",
      explicit_override: "由主机管理员设置。",
      local_selection: "使用你选择的文件夹。",
      python_prefix: "在此 ComfyUI 安装中找到。",
      system_path: "在此电脑上找到。",
    },
    needed: {
      settings: "导入、剪辑预览与最终输出需要媒体工具。",
      import: "导入需要媒体工具。",
      preview: "剪辑预览需要媒体工具。",
      derivatives: "准备剪辑需要媒体工具。",
      assembly: "组装序列需要媒体工具。",
      render: "最终输出需要媒体工具。",
    },
    details: "{source}，约 {size} MB，{license}。",
    releasePage: "发布页面",
    install: "安装",
    installAndContinue: "安装并继续",
    installAgain: "重新安装",
    cancel: "取消安装",
    progress: "安装进度",
    progressBytes: "{done} / {total} MB",
    phase: {
      checking: "正在检查下载…",
      downloading: "正在下载媒体工具…",
      extracting: "正在解压媒体工具…",
      verifying: "正在验证媒体工具…",
      publishing: "正在安装媒体工具…",
      activating: "正在启动媒体工具…",
      done: "正在完成安装…",
    },
    failed: "安装未完成。",
    adminControl: "请在 ComfyUI 主机上更改媒体工具设置，然后再次检查。",
    starting: "媒体工具正在启动…",
    renderUnavailable: "此主机无法提供最终输出。",
    recovery: "较早的媒体工具副本已另行保留。",
    recoveryKept: "在确认当前的媒体工具就绪之前，会保留这份副本。",
    reclaim: "移除较早的副本",
    advanced: "高级",
    folder: "媒体工具文件夹",
    useFolder: "使用此文件夹",
    restoreAuto: "使用自动设置",
    dismiss: "关闭",
    notice: {
      continued: "媒体工具已就绪，正在继续。",
      select_again: "媒体工具已就绪，但选择内容已更改，请重新选择。",
      continuation_cleared: "已清除等待中的操作。",
      continuation_replaced: "较早等待中的操作已被此操作取代。",
      feature_not_ready: "安装已完成，但此功能仍不可用。",
      outcome_unknown: "无法确认安装请求的结果，请再次检查。",
    },
  },
} as const;

export type MediaToolsPlacement =
  "settings" | "contextual-sidebar" | "contextual-overlay";

type MediaToolsView =
  | Readonly<{ kind: "checking" }>
  | Readonly<{ kind: "status_unavailable" }>
  | Readonly<{ kind: "installing"; job: MediaRuntimeJob }>
  | Readonly<{ kind: "ready" }>
  | Readonly<{ kind: "setup_required" }>
  | Readonly<{ kind: "failed"; reason: string | null }>
  | Readonly<{ kind: "starting" }>
  | Readonly<{ kind: "render_unavailable" }>
  | Readonly<{ kind: "unavailable"; reason: string | null }>;

const STARTING_REASONS: ReadonlySet<string> = new Set([
  "activating",
  "discovering",
  "discovery_in_progress",
]);

function fill(value: string, values: Record<string, string | number>): string {
  let output = value;
  for (const [key, replacement] of Object.entries(values))
    output = output.replace(`{${key}}`, String(replacement));
  return output;
}

function megabytes(bytes: number): string {
  return String(Math.max(1, Math.round(bytes / (1024 * 1024))));
}

function runningJob(state: MediaRuntimeState): MediaRuntimeJob | null {
  // IMPORTANT: the session's job is never older than the wire's `setup` (a status read adopts it,
  // and only later poll or action results replace it). Falling back to the wire while the session
  // holds a terminal job would show a job the user just cancelled as still installing.
  if (state.job !== null)
    return state.job.state === "running" ? state.job : null;
  return state.wire?.setup?.state === "running" ? state.wire.setup : null;
}

/** The readiness a placement is about; Settings summarizes the features the card can repair. */
function readinessFor(
  wire: MediaRuntimeStatus,
  feature: MediaRuntimeFeature | undefined,
): MediaRuntimeFeatureReadiness {
  if (feature !== undefined) return wire.features[feature];
  const rows = MEDIA_RUNTIME_FEATURES.map((name) => wire.features[name]).filter(
    (row) => row.reason !== "render_qualification_unavailable",
  );
  return (
    rows.find((row) => row.state === "setup_required") ??
    rows.find((row) => row.state !== "ready") ??
    wire.features.import
  );
}

/** One view per state; a failed read never shows the last good status as ready. */
export function deriveMediaToolsView(
  state: MediaRuntimeState,
  feature?: MediaRuntimeFeature,
): MediaToolsView {
  const job = runningJob(state);
  if (job !== null) return { kind: "installing", job };
  if (state.status === "unavailable") return { kind: "status_unavailable" };
  const wire = state.wire;
  if (wire === null) return { kind: "checking" };
  const readiness = readinessFor(wire, feature);
  if (readiness.state === "ready") return { kind: "ready" };
  if (state.job?.state === "failed")
    return { kind: "failed", reason: state.job.reason };
  if (readiness.state === "setup_required") return { kind: "setup_required" };
  if (readiness.reason === "render_qualification_unavailable")
    return { kind: "render_unavailable" };
  if (readiness.reason !== null && STARTING_REASONS.has(readiness.reason))
    return { kind: "starting" };
  return { kind: "unavailable", reason: readiness.reason };
}

/**
 * Whether a contextual surface shows the card. `blocked` is the surface's own runtime refusal;
 * `proactive` lets a surface show a known `setup_required` before the user is refused.
 */
export function mediaToolsCardVisible(
  state: MediaRuntimeState,
  feature: MediaRuntimeFeature,
  blocked: boolean,
  proactive = false,
): boolean {
  const readiness =
    state.status === "unavailable" ? undefined : state.wire?.features[feature];
  const needsSetup = proactive && readiness?.state === "setup_required";
  if (runningJob(state) !== null)
    return (
      blocked ||
      needsSetup ||
      (state.pending !== null &&
        MEDIA_RUNTIME_INTENT_FEATURE[state.pending.kind] === feature)
    );
  if (!blocked) return needsSetup;
  return (
    readiness?.state !== "ready" ||
    state.notice !== null ||
    state.refusal !== null
  );
}

export function MediaToolsCard({
  placement,
  binding,
  locale,
  feature,
  intent = null,
}: {
  placement: MediaToolsPlacement;
  binding: MediaToolsBinding;
  locale: Locale;
  /** The feature a contextual placement is blocked on; Settings leaves it unset. */
  feature?: MediaRuntimeFeature;
  /** The original action to continue after setup; `null` installs without continuing. */
  intent?: MediaRuntimeIntent | null;
}) {
  const text = copy[locale];
  const titleId = useId();
  const folderId = useId();
  const { state } = binding;
  const view = deriveMediaToolsView(state, feature);
  const wire = state.wire;
  const settings = placement === "settings";
  const statusLine = useRef<HTMLParagraphElement | null>(null);
  const primary = useRef<HTMLButtonElement | null>(null);
  // Focus is moved only for a card the user acted in, so two cards visible in one overlay never
  // compete and an unrelated status change never takes focus from where the user is.
  const engaged = useRef(false);
  const [folder, setFolder] = useState("");

  // Reads happen when a surface can use them: once on the first Settings open, and whenever a
  // contextual card mounts because its surface was refused by the runtime.
  const ensure = binding.ensure;
  const report = binding.report;
  useEffect(() => {
    if (feature === undefined) ensure();
    else report(feature);
  }, [ensure, report, feature]);

  const installing = view.kind === "installing";
  useEffect(() => {
    if (installing && engaged.current) statusLine.current?.focus();
  }, [installing]);

  const focusPrimary = state.focusPrimary;
  const firstFocus = useRef(focusPrimary);
  useEffect(() => {
    if (focusPrimary === firstFocus.current || !engaged.current) return;
    (primary.current ?? statusLine.current)?.focus();
  }, [focusPrimary]);

  const busy = state.busy !== null;
  const offers = (action: MediaRuntimeStatus["actions"][number]) =>
    wire?.actions.includes(action) === true;
  const install = () => binding.install(settings ? null : intent);

  let message: string;
  let detail: string | null = null;
  let action: Readonly<{ label: string; run(): void }> | null = null;
  switch (view.kind) {
    case "checking":
      message = text.checking;
      break;
    case "status_unavailable":
      message = text.statusUnavailable;
      action = { label: text.checkAgain, run: binding.rescan };
      break;
    case "installing":
      message = text.phase[view.job.phase];
      break;
    case "ready":
      message = text.ready;
      if (settings && wire !== null && wire.resolution.sourceKind !== "none")
        detail = text.source[wire.resolution.sourceKind];
      break;
    case "setup_required":
      message = text.needed[feature ?? "settings"];
      if (offers("install_supported"))
        action = {
          label: settings ? text.install : text.installAndContinue,
          run: install,
        };
      break;
    case "failed":
      message = text.failed;
      detail =
        view.reason === "advanced_override_active"
          ? text.adminControl
          : plainReason(locale, "mediaRuntime", view.reason);
      if (
        view.reason !== "advanced_override_active" &&
        offers("install_supported")
      )
        action = { label: text.installAgain, run: install };
      break;
    case "starting":
      message = text.starting;
      break;
    case "render_unavailable":
      message = text.renderUnavailable;
      break;
    case "unavailable":
      message = plainReason(locale, "mediaRuntime", view.reason);
      if (offers("rescan"))
        action = { label: text.checkAgain, run: binding.rescan };
      break;
  }
  const showInstallSource =
    wire !== null && (view.kind === "setup_required" || view.kind === "failed");
  const job = view.kind === "installing" ? view.job : null;
  const Heading = settings ? "h3" : "h4";

  return (
    <section
      className={
        placement === "contextual-overlay" ? "h3-nle-media-tools" : "h3s-mt"
      }
      aria-labelledby={titleId}
      data-h3-media-tools={view.kind}
      data-h3-media-tools-placement={placement}
      data-h3-media-tools-reason={
        view.kind === "failed" || view.kind === "unavailable"
          ? (view.reason ?? undefined)
          : undefined
      }
      onClickCapture={() => {
        engaged.current = true;
      }}
    >
      <Heading id={titleId} className="h3-sec">
        {text.title}
      </Heading>
      {settings ? <p className="h3ds">{text.description}</p> : null}
      <p
        ref={statusLine}
        className="h3s-mt-state"
        role="status"
        aria-live="polite"
        tabIndex={-1}
        data-h3-focus-key={settings ? "settings-media-tools" : undefined}
      >
        <span className="h3-val">{message}</span>
        {detail !== null ? <span className="h3-meta">{detail}</span> : null}
      </p>
      {settings &&
      view.kind !== "render_unavailable" &&
      wire?.features.render.reason === "render_qualification_unavailable" ? (
        <p className="h3-meta" data-h3-media-tools-render="unavailable">
          {text.renderUnavailable}
        </p>
      ) : null}
      {job !== null ? (
        <>
          <progress
            aria-label={text.progress}
            max={job.totalBytes > 0 ? job.totalBytes : undefined}
            value={job.totalBytes > 0 ? job.completedBytes : undefined}
          />
          {job.totalBytes > 0 ? (
            <p className="h3-meta" aria-hidden="true">
              {fill(text.progressBytes, {
                done: megabytes(job.completedBytes),
                total: megabytes(job.totalBytes),
              })}
            </p>
          ) : null}
        </>
      ) : null}
      {showInstallSource ? (
        <p className="h3-meta" data-h3-media-tools-source="install">
          {fill(text.details, {
            source: wire.install.sourceLabel,
            size: megabytes(wire.install.approximateBytes),
            license: wire.install.license,
          })}{" "}
          <a
            href={wire.install.releasePage}
            target="_blank"
            rel="noopener noreferrer"
          >
            {text.releasePage}
          </a>
        </p>
      ) : null}
      {state.refusal !== null && view.kind !== "status_unavailable" ? (
        <p
          role="alert"
          className="h3s-mt-rejection"
          data-h3-media-tools-refusal={state.refusal}
        >
          {plainReason(locale, "mediaRuntime", state.refusal)}
        </p>
      ) : null}
      {state.notice !== null ? (
        <p
          className="h3s-mt-notice"
          role="status"
          data-h3-media-tools-notice={state.notice}
        >
          <span>{text.notice[state.notice]}</span>
          <button type="button" onClick={binding.dismiss}>
            {text.dismiss}
          </button>
        </p>
      ) : null}
      {action !== null ? (
        <button
          ref={primary}
          type="button"
          data-h3-focus-key="media-tools-primary"
          data-h3-media-tools-action="primary"
          disabled={busy}
          onClick={action.run}
        >
          {action.label}
        </button>
      ) : null}
      {job !== null ? (
        <button
          ref={primary}
          type="button"
          data-h3-focus-key="media-tools-primary"
          data-h3-media-tools-action="cancel"
          // IMPORTANT: a job this page started arrives from the install reply, not a status read,
          // so the last status predates it and never lists `cancel_setup`. Only a status that
          // describes this same running job may withhold Cancel; otherwise Cancel is dead.
          disabled={
            busy ||
            (wire?.setup?.jobId === job.jobId &&
              wire.setup.state === "running" &&
              !offers("cancel_setup"))
          }
          onClick={binding.cancel}
        >
          {text.cancel}
        </button>
      ) : null}
      {settings && wire?.recovery != null && job === null ? (
        <div className="h3s-mt-recovery" data-h3-media-tools-recovery="true">
          <p>{text.recovery}</p>
          {wire.recovery.reclaimable && offers("reclaim_parked_runtime") ? (
            <button type="button" disabled={busy} onClick={binding.reclaim}>
              {text.reclaim}
            </button>
          ) : (
            <p className="h3-meta">{text.recoveryKept}</p>
          )}
        </div>
      ) : null}
      {settings && wire !== null && job === null ? (
        <details className="h3s-mt-advanced">
          <summary>{text.advanced}</summary>
          <div className="h3s-r">
            <label htmlFor={folderId}>{text.folder}</label>
            <input
              id={folderId}
              type="text"
              value={folder}
              maxLength={4096}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setFolder(event.currentTarget.value)}
            />
          </div>
          <button
            type="button"
            disabled={
              busy || folder.trim() === "" || !offers("use_local_directory")
            }
            onClick={() => binding.useLocalDirectory(folder.trim())}
          >
            {text.useFolder}
          </button>
          {offers("restore_auto") ? (
            <button type="button" disabled={busy} onClick={binding.restoreAuto}>
              {text.restoreAuto}
            </button>
          ) : null}
        </details>
      ) : null}
    </section>
  );
}
