import { useEffect, useId, useRef, useState } from "react";
import type { Locale } from "../i18n/catalog";
import {
  MEDIA_RUNTIME_FEATURES,
  type MediaRuntimeFeature,
  type MediaRuntimeStatus,
} from "../contracts/mediaRuntimeCodec";
import type {
  MediaRuntimeIntent,
  MediaRuntimeState,
  MediaToolsBinding,
} from "../state/mediaRuntimeState";
import { plainReason } from "./plainReasons";
import mediaToolsStyles from "./mediaToolsCard.css?inline";

export const mediaToolsCopy = {
  en: {
    title: "Media tools",
    description:
      "Import, preview and final video use a supported FFmpeg and FFprobe pair.",
    needed: "Set up media tools to use this feature.",
    ready: "Media tools are ready.",
    manual:
      "Open the release page, download the exact archive below and extract it yourself. Then enter its bin folder, which contains ffmpeg.exe and ffprobe.exe.",
    releasePage: "Open download page",
    help: "FFmpeg setup help",
    closeHelp: "Close help",
    version: "Pinned version",
    license: "Licence",
    helpDownload:
      "Open the download page and choose the exact archive shown here.",
    helpVerify:
      "Before extracting, compare the archive's SHA-256 with the value shown here. On Windows, use either command, replacing <file> with the archive path:",
    helpExtract: "Extract the verified archive yourself.",
    helpSelect:
      "Enter its bin folder containing ffmpeg.exe and ffprobe.exe, or add that folder to PATH, then choose Check again.",
    helpPins:
      "Only the pinned FFmpeg and FFprobe executables are supported. A different build is refused because its executable checksums do not match.",
    helpEarlier: "A verified setup made by an earlier version keeps working.",
    archive: "Archive",
    checksum: "Archive SHA-256",
    copy: "Copy SHA-256",
    copied: "Copied",
    copyFailed: "Could not copy. Select and copy the checksum.",
    folder: "FFmpeg bin folder",
    useFolder: "Use folder",
    path: "You can also place the supported pair on PATH, then check again.",
    checkAgain: "Check again",
    advanced: "Advanced",
    restoreAuto: "Search automatically",
    retry: "Tools are selected. Retry the original action when you are ready.",
    checking: "Checking media tools…",
    unavailable: "Media tool status is unavailable.",
    renderUnavailable:
      "Final video is unavailable on this host. Selecting tools cannot enable its renderer.",
    recovery: "Earlier setup left temporary files or an earlier copy behind.",
    reclaim: "Clean up earlier setup files",
    recoveryKept:
      "The earlier copy is kept until a supported managed replacement is verified.",
    source: {
      managed: "Set up by an earlier version",
      explicit_override: "Host override",
      local_selection: "Selected folder",
      python_prefix: "Python environment",
      system_path: "System PATH",
      none: "No source selected",
    },
  },
  "zh-TW": {
    title: "媒體工具",
    description: "匯入、預覽與最終影片使用支援的 FFmpeg 與 FFprobe 工具組。",
    needed: "請設定媒體工具以使用此功能。",
    ready: "媒體工具已就緒。",
    manual:
      "開啟發行頁面，下載下方指定的壓縮檔並自行解壓縮，再輸入包含 ffmpeg.exe 與 ffprobe.exe 的 bin 資料夾。",
    releasePage: "開啟下載網址",
    help: "FFmpeg 設定說明",
    closeHelp: "關閉說明",
    version: "指定版本",
    license: "授權",
    helpDownload: "開啟下載網址，選擇此處顯示的指定壓縮檔。",
    helpVerify:
      "解壓縮前，請將壓縮檔的 SHA-256 與此處的校驗碼比對。在 Windows 可使用以下任一指令，將 <file> 換成壓縮檔路徑：",
    helpExtract: "自行解壓縮已驗證的壓縮檔。",
    helpSelect:
      "輸入包含 ffmpeg.exe 與 ffprobe.exe 的 bin 資料夾，或將該資料夾加入 PATH，再按「重新檢查」。",
    helpPins:
      "僅支援指定的 FFmpeg 與 FFprobe 執行檔。其他版本的執行檔校驗碼不符，因此無法使用。",
    helpEarlier: "由先前版本完成且通過驗證的設定可繼續使用。",
    archive: "壓縮檔",
    checksum: "壓縮檔 SHA-256",
    copy: "複製 SHA-256",
    copied: "已複製",
    copyFailed: "無法複製，請選取並複製校驗碼。",
    folder: "FFmpeg bin 資料夾",
    useFolder: "使用資料夾",
    path: "也可將支援的工具組放入 PATH，再重新檢查。",
    checkAgain: "重新檢查",
    advanced: "進階",
    restoreAuto: "自動搜尋",
    retry: "工具已選定，準備好後請重試原本的操作。",
    checking: "正在檢查媒體工具…",
    unavailable: "無法讀取媒體工具狀態。",
    renderUnavailable: "此主機無法輸出最終影片，選擇工具無法啟用其渲染器。",
    recovery: "先前的設定留下了暫存檔或舊版工具。",
    reclaim: "清理先前設定的檔案",
    recoveryKept: "在確認受支援的替代工具前，會保留舊版工具。",
    source: {
      managed: "由先前版本設定",
      explicit_override: "主機指定",
      local_selection: "選定的資料夾",
      python_prefix: "Python 環境",
      system_path: "系統 PATH",
      none: "尚未選定來源",
    },
  },
  "zh-CN": {
    title: "媒体工具",
    description: "导入、预览与最终视频使用支持的 FFmpeg 与 FFprobe 工具组。",
    needed: "请设置媒体工具以使用此功能。",
    ready: "媒体工具已就绪。",
    manual:
      "打开发布页面，下载下方指定的压缩文件并自行解压，再输入包含 ffmpeg.exe 与 ffprobe.exe 的 bin 文件夹。",
    releasePage: "打开下载网址",
    help: "FFmpeg 设置说明",
    closeHelp: "关闭说明",
    version: "指定版本",
    license: "许可",
    helpDownload: "打开下载网址，选择此处显示的指定压缩文件。",
    helpVerify:
      "解压前，请将压缩文件的 SHA-256 与此处的校验码比对。在 Windows 可使用以下任一命令，将 <file> 换成压缩文件路径：",
    helpExtract: "自行解压已验证的压缩文件。",
    helpSelect:
      "输入包含 ffmpeg.exe 与 ffprobe.exe 的 bin 文件夹，或将该文件夹加入 PATH，再点击“重新检查”。",
    helpPins:
      "仅支持指定的 FFmpeg 与 FFprobe 可执行文件。其他版本的可执行文件校验码不符，因此无法使用。",
    helpEarlier: "由先前版本完成且通过验证的设置可继续使用。",
    archive: "压缩文件",
    checksum: "压缩文件 SHA-256",
    copy: "复制 SHA-256",
    copied: "已复制",
    copyFailed: "无法复制，请选中并复制校验码。",
    folder: "FFmpeg bin 文件夹",
    useFolder: "使用文件夹",
    path: "也可将支持的工具组放入 PATH，再重新检查。",
    checkAgain: "重新检查",
    advanced: "高级",
    restoreAuto: "自动搜索",
    retry: "工具已选定，准备好后请重试原来的操作。",
    checking: "正在检查媒体工具…",
    unavailable: "无法读取媒体工具状态。",
    renderUnavailable: "此主机无法输出最终视频，选择工具无法启用其渲染器。",
    recovery: "先前的设置留下了临时文件或旧版工具。",
    reclaim: "清理先前设置的文件",
    recoveryKept: "在确认支持的替代工具前，会保留旧版工具。",
    source: {
      managed: "由先前版本设置",
      explicit_override: "主机指定",
      local_selection: "选定的文件夹",
      python_prefix: "Python 环境",
      system_path: "系统 PATH",
      none: "尚未选择来源",
    },
  },
} as const;

export type MediaToolsPlacement =
  "settings" | "contextual-sidebar" | "contextual-overlay";
export type MediaToolsView = Readonly<{
  kind:
    | "checking"
    | "status_unavailable"
    | "ready"
    | "setup_required"
    | "render_unavailable"
    | "starting"
    | "unavailable";
  reason?: string | null;
}>;

function readiness(wire: MediaRuntimeStatus, feature?: MediaRuntimeFeature) {
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
export function deriveMediaToolsView(
  state: MediaRuntimeState,
  feature?: MediaRuntimeFeature,
): MediaToolsView {
  if (state.status === "unavailable") return { kind: "status_unavailable" };
  if (state.wire === null) return { kind: "checking" };
  const row = readiness(state.wire, feature);
  if (row.state === "ready") return { kind: "ready" };
  if (row.state === "setup_required") return { kind: "setup_required" };
  if (row.reason === "render_qualification_unavailable")
    return { kind: "render_unavailable" };
  if (
    ["activating", "discovering", "discovery_in_progress"].includes(
      row.reason ?? "",
    )
  )
    return { kind: "starting" };
  return { kind: "unavailable", reason: row.reason };
}
export function mediaToolsCardVisible(
  state: MediaRuntimeState,
  feature: MediaRuntimeFeature,
  blocked: boolean,
  proactive = false,
): boolean {
  const row =
    state.status === "unavailable" ? undefined : state.wire?.features[feature];
  return (
    (proactive && row?.state === "setup_required") ||
    (blocked && (row?.state !== "ready" || state.refusal !== null))
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
  feature?: MediaRuntimeFeature;
  intent?: MediaRuntimeIntent | null;
}) {
  const text = mediaToolsCopy[locale];
  const { state } = binding;
  const view = deriveMediaToolsView(state, feature);
  const wire = state.wire;
  const settings = placement === "settings";
  const titleId = useId();
  const folderId = useId();
  const helpId = useId();
  const helpTitleId = useId();
  const [helpOpen, setHelpOpen] = useState(false);
  const helpTrigger = useRef<HTMLButtonElement>(null);
  const helpPanel = useRef<HTMLDivElement>(null);
  const [folder, setFolder] = useState("");
  const [copyState, setCopyState] = useState<"copied" | "copyFailed" | null>(
    null,
  );
  const primary = useRef<HTMLButtonElement>(null);
  const engaged = useRef(false);
  useEffect(() => {
    if (helpOpen) helpPanel.current?.focus();
  }, [helpOpen]);
  const closeHelp = () => {
    setHelpOpen(false);
    helpTrigger.current?.focus();
  };
  const ensure = binding.ensure;
  const report = binding.report;
  useEffect(() => {
    if (feature === undefined) ensure();
    else report(feature);
  }, [ensure, report, feature]);
  const initialFocus = useRef(state.focusPrimary);
  useEffect(() => {
    if (state.focusPrimary !== initialFocus.current && engaged.current)
      primary.current?.focus();
  }, [state.focusPrimary]);
  const offers = (
    action: "use_local_directory" | "restore_auto" | "reclaim_parked_runtime",
  ) =>
    state.status !== "unavailable" && wire?.actions.includes(action) === true;
  const busy = state.busy !== null;
  const manualVisible =
    wire !== null &&
    (settings ||
      view.kind === "setup_required" ||
      view.kind === "unavailable") &&
    view.kind !== "render_unavailable";
  const message =
    view.kind === "ready"
      ? text.ready
      : view.kind === "setup_required"
        ? text.needed
        : view.kind === "render_unavailable"
          ? text.renderUnavailable
          : view.kind === "checking" || view.kind === "starting"
            ? text.checking
            : text.unavailable;
  const Heading = settings ? "h3" : "h4";
  return (
    <section
      className={
        placement === "contextual-overlay" ? "h3-nle-media-tools" : "h3s-mt"
      }
      aria-labelledby={titleId}
      data-h3-media-tools={view.kind}
      data-h3-media-tools-placement={placement}
      data-h3-media-tools-context={intent?.kind}
      onClickCapture={() => {
        engaged.current = true;
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && helpOpen) {
          // Keep help dismissal local so Escape does not also close the enclosing editor.
          event.preventDefault();
          event.stopPropagation();
          closeHelp();
        }
      }}
    >
      <style>{mediaToolsStyles}</style>
      <div className="h3s-mt-heading">
        <Heading id={titleId} className="h3-sec">
          {text.title}
        </Heading>
        <button
          ref={helpTrigger}
          type="button"
          className="h3-icon-button h3s-mt-help-toggle"
          aria-label={text.help}
          title={text.help}
          aria-expanded={helpOpen}
          aria-controls={helpId}
          disabled={wire === null}
          data-h3-media-tools-action="help"
          onClick={() => (helpOpen ? closeHelp() : setHelpOpen(true))}
        >
          <span aria-hidden="true">?</span>
        </button>
      </div>
      {helpOpen && wire ? (
        <div
          ref={helpPanel}
          id={helpId}
          role="region"
          aria-labelledby={helpTitleId}
          tabIndex={-1}
          className="h3s-mt-help"
          data-h3-media-tools-help
        >
          <h4 id={helpTitleId}>{text.help}</h4>
          <p>{text.description}</p>
          <dl>
            <dt>{text.version}</dt>
            <dd>
              <code>{wire.manual.releasePage.split("/").at(-1)}</code>
            </dd>
            <dt>{text.archive}</dt>
            <dd>
              <code>{wire.manual.archiveName}</code>
            </dd>
            <dt>{text.checksum}</dt>
            <dd>
              <code>{wire.manual.archiveSha256}</code>
            </dd>
            <dt>{text.license}</dt>
            <dd>{wire.manual.license}</dd>
          </dl>
          <ol>
            <li>{text.helpDownload}</li>
            <li>
              {text.helpVerify}
              <code className="h3s-mt-command">
                Get-FileHash -Algorithm SHA256 &lt;file&gt;
              </code>
              <code className="h3s-mt-command">
                certutil -hashfile &lt;file&gt; SHA256
              </code>
            </li>
            <li>{text.helpExtract}</li>
            <li>{text.helpSelect}</li>
          </ol>
          <p>{text.helpPins}</p>
          <p>{text.helpEarlier}</p>
          <button
            type="button"
            data-h3-media-tools-action="close-help"
            onClick={closeHelp}
          >
            {text.closeHelp}
          </button>
        </div>
      ) : null}
      {settings ? <p className="h3ds">{text.description}</p> : null}
      <p
        className="h3s-mt-state"
        role="status"
        aria-live="polite"
        data-h3-focus-key={settings ? "settings-media-tools" : undefined}
        tabIndex={-1}
      >
        <span className="h3-val">{message}</span>
        {view.kind === "ready" && wire ? (
          <span className="h3-meta">
            {text.source[wire.resolution.sourceKind]}
          </span>
        ) : null}
      </p>
      {settings &&
      wire?.features.render.reason === "render_qualification_unavailable" ? (
        <p className="h3-meta" data-h3-media-tools-render="unavailable">
          {text.renderUnavailable}
        </p>
      ) : null}
      {manualVisible && wire ? (
        <div data-h3-media-tools-source="manual">
          <p>{text.manual}</p>
          <p className="h3-meta">
            {wire.manual.sourceLabel} ·{" "}
            {Math.round(wire.manual.approximateBytes / 1048576)} MB ·{" "}
            {wire.manual.license}
          </p>
          <a
            className="h3s-mt-download"
            data-variant="primary"
            data-h3-media-tools-action="download"
            href={wire.manual.releasePage}
            target="_blank"
            rel="noopener noreferrer"
          >
            {text.releasePage}
          </a>
          <p style={{ overflowWrap: "anywhere" }}>
            {text.archive}: <code>{wire.manual.archiveName}</code>
          </p>
          <p style={{ overflowWrap: "anywhere" }}>
            {text.checksum}:{" "}
            <code data-h3-media-tools-sha>{wire.manual.archiveSha256}</code>
          </p>
          <button
            type="button"
            data-h3-media-tools-action="copy"
            onClick={() => {
              // Clipboard access can be absent on a host served outside a secure browser context.
              void Promise.resolve()
                .then(() =>
                  navigator.clipboard.writeText(wire.manual.archiveSha256),
                )
                .then(
                  () => setCopyState("copied"),
                  () => setCopyState("copyFailed"),
                );
            }}
          >
            {text.copy}
          </button>
          {copyState ? <p role="status">{text[copyState]}</p> : null}
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
            ref={primary}
            type="button"
            data-h3-focus-key="media-tools-primary"
            data-h3-media-tools-action="primary"
            disabled={
              busy || folder.trim() === "" || !offers("use_local_directory")
            }
            onClick={() => binding.useLocalDirectory(folder.trim())}
          >
            {text.useFolder}
          </button>
          <p className="h3-meta">{text.path}</p>
        </div>
      ) : null}
      {view.kind !== "render_unavailable" ? (
        <button
          type="button"
          disabled={busy}
          data-h3-media-tools-action="rescan"
          onClick={binding.rescan}
        >
          {text.checkAgain}
        </button>
      ) : null}
      {state.refusal !== null ? (
        <p
          role="alert"
          className="h3s-mt-rejection"
          data-h3-media-tools-refusal={state.refusal}
        >
          {plainReason(locale, "mediaRuntime", state.refusal)}
        </p>
      ) : null}
      {state.notice !== null ? (
        <p role="status" data-h3-media-tools-notice={state.notice}>
          {text.retry}
        </p>
      ) : null}
      {settings && wire?.recovery ? (
        <div className="h3s-mt-recovery" data-h3-media-tools-recovery="true">
          <p>{text.recovery}</p>
          {wire.recovery.reclaimable && offers("reclaim_parked_runtime") ? (
            <button type="button" disabled={busy} onClick={binding.reclaim}>
              {text.reclaim}
            </button>
          ) : (
            <p>{text.recoveryKept}</p>
          )}
        </div>
      ) : null}
      {manualVisible && wire ? (
        <details className="h3s-mt-advanced">
          <summary>{text.advanced}</summary>
          {offers("restore_auto") ? (
            <button type="button" disabled={busy} onClick={binding.restoreAuto}>
              {text.restoreAuto}
            </button>
          ) : (
            <p className="h3-meta">{text.path}</p>
          )}
        </details>
      ) : null}
    </section>
  );
}
