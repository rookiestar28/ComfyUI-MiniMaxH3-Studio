import { useState } from "react";
import type { Locale } from "../i18n/catalog";
import type { ProjectRecoveryBinding } from "../lifecycle/projectRecoverySession";
import {
  NleActionIcon,
  NleIconButton,
  NleIconGroup,
} from "./nle/NleIconActions";

const COPY = {
  en: {
    recovery: "Server recovery",
    enabled: "Save draft recovery",
    video: "Include retained video links",
    disclosure:
      "Opting in stores draft prompts, titles, editable text and undo/redo on this server. Closed recoveries are kept for seven days. Edits without a saved acknowledgement may be lost.",
    videoDisclosure:
      "Video links include only verified retained media. This does not enable media retention or copy unretained media.",
    now: "Save recovery now",
    refresh: "Refresh recoveries",
    restore: "Restore as a new project",
    clear: "Clear recovery",
    choose: "Select recovery",
    confirmClear:
      "Clear this saved recovery? Exported files and other projects are unchanged.",
    discard: "Discard unsaved recovery edits",
    confirmDiscard:
      "Stop tracking these unsaved edits? The last saved recovery remains. New edits can start recovery again.",
    off: "Recovery off",
    idle: "No active recovery",
    dirty: "Recovery has unsaved edits",
    saving: "Saving recovery",
    saved: "Recovery saved",
    error: "Recovery unavailable; edits are not confirmed saved",
  },
  "zh-TW": {
    recovery: "伺服器恢復",
    enabled: "儲存草稿恢復資料",
    video: "包含保留影片連結",
    disclosure:
      "啟用後，草稿提示詞、名稱、編輯文字與復原／重做會儲存在此伺服器。已關閉的恢復資料保留七天；尚未確認儲存的編輯可能遺失。",
    videoDisclosure:
      "影片連結僅包含驗證過的保留媒體，不會啟用媒體保留，也不會複製未保留的媒體。",
    now: "立即儲存恢復資料",
    refresh: "重新讀取恢復資料",
    restore: "恢復為新專案",
    clear: "清除恢復資料",
    choose: "選擇恢復資料",
    confirmClear: "清除此份已儲存的恢復資料？匯出檔案與其他專案不受影響。",
    discard: "捨棄尚未儲存的恢復編輯",
    confirmDiscard:
      "停止追蹤這些尚未儲存的編輯？最後儲存的恢復資料仍會保留；新編輯可再次啟動恢復儲存。",
    off: "恢復儲存已關閉",
    idle: "目前未追蹤恢復資料",
    dirty: "恢復資料有未儲存的編輯",
    saving: "正在儲存恢復資料",
    saved: "恢復資料已儲存",
    error: "恢復儲存無法使用；編輯尚未確認儲存",
  },
  "zh-CN": {
    recovery: "服务器恢复",
    enabled: "保存草稿恢复数据",
    video: "包含保留视频链接",
    disclosure:
      "启用后，草稿提示词、名称、编辑文字与撤销／重做会保存在此服务器。已关闭的恢复数据保留七天；尚未确认保存的编辑可能丢失。",
    videoDisclosure:
      "视频链接仅包含验证过的保留媒体，不会启用媒体保留，也不会复制未保留的媒体。",
    now: "立即保存恢复数据",
    refresh: "重新读取恢复数据",
    restore: "恢复为新项目",
    clear: "清除恢复数据",
    choose: "选择恢复数据",
    confirmClear: "清除此份已保存的恢复数据？导出文件与其他项目不受影响。",
    discard: "放弃尚未保存的恢复编辑",
    confirmDiscard:
      "停止跟踪这些尚未保存的编辑？最后保存的恢复数据仍会保留；新编辑可再次启动恢复保存。",
    off: "恢复保存已关闭",
    idle: "当前未跟踪恢复数据",
    dirty: "恢复数据有未保存的编辑",
    saving: "正在保存恢复数据",
    saved: "恢复数据已保存",
    error: "恢复保存不可用；编辑尚未确认保存",
  },
} as const;

export function ProjectRecoveryControls({
  binding,
  locale,
  blocked = false,
}: {
  binding?: ProjectRecoveryBinding;
  locale: Locale;
  blocked?: boolean;
}) {
  const [selected, select] = useState("");
  if (binding === undefined) return null;
  const text = COPY[locale],
    { state } = binding,
    busy = state.busy || blocked;
  const record = state.records.find((item) => item.project_id === selected);
  return (
    <section
      className="h3-project-recovery"
      data-h3-project-recovery=""
      aria-label={text.recovery}
    >
      <p className="h3-meta">{text.disclosure}</p>
      <label className="h3-project-recovery-toggle">
        <input
          type="checkbox"
          checked={state.enabled}
          disabled={busy}
          onChange={(event) => {
            void binding.settings(event.target.checked, state.includeVideo);
          }}
        />
        {text.enabled}
      </label>
      <label className="h3-project-recovery-toggle">
        <input
          type="checkbox"
          checked={state.includeVideo}
          disabled={busy || !state.enabled}
          onChange={(event) => {
            void binding.settings(state.enabled, event.target.checked);
          }}
        />
        {text.video}
      </label>
      <p className="h3-meta">{text.videoDisclosure}</p>
      <p
        role={state.status === "error" ? "alert" : "status"}
        aria-live="polite"
        data-h3-recovery-status={state.status}
      >
        {text[state.status]}
      </p>
      <NleIconGroup label={text.recovery}>
        <NleIconButton
          icon="export"
          hue="ok"
          kind="action"
          control="save-project-recovery"
          label={text.now}
          description={text.now}
          disabled={busy || !state.enabled}
          onActivate={() => {
            void binding.saveNow();
          }}
        />
        <NleIconButton
          icon="refresh"
          hue="info"
          kind="action"
          control="refresh-project-recovery"
          label={text.refresh}
          description={text.refresh}
          disabled={busy}
          onActivate={() => {
            void binding.refresh();
          }}
        />
        <NleIconButton
          icon="delete"
          hue="audit"
          kind="action"
          control="discard-project-recovery"
          label={text.discard}
          description={text.discard}
          disabled={busy || !state.dirty}
          onActivate={() => {
            if (globalThis.confirm(text.confirmDiscard))
              void binding.beforeReplace(true);
          }}
        />
      </NleIconGroup>
      {state.records.length === 0 ? null : (
        <div className="h3-project-recovery-catalog">
          <label>
            {text.choose}
            <select
              aria-label={text.choose}
              value={record === undefined ? "" : selected}
              disabled={busy}
              onChange={(event) => select(event.target.value)}
            >
              <option value="">{text.choose}</option>
              {state.records.map((item) => (
                <option key={item.project_id} value={item.project_id}>
                  {new Date(item.saved_at_ms).toLocaleString(locale)} (
                  {item.project_id.slice(-8)})
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            title={text.restore}
            aria-label={text.restore}
            disabled={busy || record === undefined}
            onClick={() => {
              if (record !== undefined) void binding.restore(record.project_id);
            }}
          >
            <NleActionIcon name="import" />
          </button>
          <button
            type="button"
            title={text.clear}
            aria-label={text.clear}
            disabled={busy || record === undefined || record.active}
            onClick={() => {
              if (record !== undefined && globalThis.confirm(text.confirmClear))
                void binding.clear(record.project_id);
            }}
          >
            <NleActionIcon name="delete" />
          </button>
        </div>
      )}
    </section>
  );
}
