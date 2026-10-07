import { useRef, useState } from "react";
import type { Locale } from "../i18n/catalog";
import { ProjectRecoveryControls } from "./ProjectRecoveryControls";
import type {
  ProjectFileBinding,
  ProjectSavePicker,
} from "../lifecycle/projectFileSession";
import {
  NleActionIcon,
  NleIconButton,
  NleIconGroup,
} from "./nle/NleIconActions";
const COPY = {
  en: {
    project: "Project file",
    title: "Project title",
    save: "Save project",
    open: "Open project",
    download: "Download project",
    privacy:
      "Project files include your draft prompts and editable text. Media is not included.",
    confirm:
      "Open as a new project? Current edits remain in the previous project.",
    previous: "Previous project",
    missing: "Missing media",
    choose: "Select retained video",
    refresh: "Refresh retained videos",
    relink: "Relink media",
    cancel: "Cancel",
    idle: "Not exported",
    saving: "Writing project",
    saved: "File written",
    downloadStatus: "Download prepared",
    cancelled: "Cancelled",
    opening: "Opening project",
    opened: "Project opened",
    relinking: "Verifying media",
    error: "Project operation failed",
    uncertain:
      "Media link outcome is unknown. Open the saved file as a new project before continuing.",
    dirty: "Unsaved edits",
  },
  "zh-TW": {
    project: "專案檔案",
    title: "專案名稱",
    save: "儲存專案",
    open: "開啟專案",
    download: "下載專案",
    privacy: "專案檔案包含草稿提示詞與編輯文字，不包含媒體。",
    confirm: "以新專案開啟？目前編輯會保留在上一個專案。",
    previous: "上一個專案",
    missing: "缺少媒體",
    choose: "選擇保留的影片",
    refresh: "重新讀取保留影片",
    relink: "重新連結媒體",
    cancel: "取消",
    idle: "尚未匯出",
    saving: "正在寫入專案",
    saved: "檔案已寫入",
    downloadStatus: "下載已準備",
    cancelled: "已取消",
    opening: "正在開啟專案",
    opened: "專案已開啟",
    relinking: "正在驗證媒體",
    error: "專案操作失敗",
    uncertain: "媒體連結結果尚未確認。請先以新專案開啟已儲存的檔案再繼續。",
    dirty: "有未儲存的編輯",
  },
  "zh-CN": {
    project: "项目文件",
    title: "项目名称",
    save: "保存项目",
    open: "打开项目",
    download: "下载项目",
    privacy: "项目文件包含草稿提示词与编辑文字，不包含媒体。",
    confirm: "作为新项目打开？当前编辑会保留在上一个项目。",
    previous: "上一个项目",
    missing: "缺少媒体",
    choose: "选择保留的视频",
    refresh: "重新读取保留视频",
    relink: "重新关联媒体",
    cancel: "取消",
    idle: "尚未导出",
    saving: "正在写入项目",
    saved: "文件已写入",
    downloadStatus: "下载已准备",
    cancelled: "已取消",
    opening: "正在打开项目",
    opened: "项目已打开",
    relinking: "正在验证媒体",
    error: "项目操作失败",
    uncertain: "媒体关联结果尚未确认。请先作为新项目打开已保存的文件再继续。",
    dirty: "有未保存的编辑",
  },
} as const;
export function ProjectFileControls({
  binding,
  locale,
  compact = false,
}: {
  binding?: ProjectFileBinding;
  locale: Locale;
  compact?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null),
    menu = useRef<HTMLDetailsElement>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  if (binding === undefined) return null;
  const text = COPY[locale],
    { state } = binding;
  const working = ["saving", "opening", "relinking"].includes(state.status);
  const busy = working || binding.blocked === true;
  const picker = (
    globalThis as typeof globalThis & { showSaveFilePicker?: ProjectSavePicker }
  ).showSaveFilePicker;
  const body = (
    <div
      className="h3-project-file"
      data-h3-project-file=""
      onKeyDown={(event) => {
        if (event.key === "Escape" && menu.current?.open) {
          event.stopPropagation();
          menu.current.open = false;
          menu.current.querySelector("summary")?.focus();
        }
      }}
    >
      <label>
        {text.title}
        <input
          aria-label={text.title}
          maxLength={256}
          value={state.title}
          disabled={busy}
          onChange={(event) => binding.setTitle(event.target.value)}
        />
      </label>
      <NleIconGroup label={text.project}>
        <NleIconButton
          icon="project"
          hue="production"
          kind="action"
          control="open-project-file"
          label={text.open}
          description={text.open}
          disabled={busy}
          onActivate={() => {
            if (!state.dirty || globalThis.confirm(text.confirm))
              input.current?.click();
          }}
        />
        <NleIconButton
          icon="export"
          hue="ok"
          kind="action"
          control="save-project-file"
          label={text.save}
          description={text.privacy}
          disabled={busy}
          onActivate={() => {
            void binding.save(picker);
          }}
        />
        {picker === undefined ? null : (
          <NleIconButton
            icon="export"
            hue="info"
            kind="action"
            control="download-project-file"
            label={text.download}
            description={text.download}
            disabled={busy}
            onActivate={() => {
              void binding.save();
            }}
          />
        )}
        {binding.previous === undefined ? null : (
          <NleIconButton
            icon="stepBack"
            hue="info"
            kind="action"
            control="previous-project-file"
            label={text.previous}
            description={text.previous}
            disabled={busy}
            onActivate={binding.previous}
          />
        )}
        {working ? (
          <NleIconButton
            icon="close"
            hue="audit"
            kind="action"
            control="cancel-project-file"
            label={text.cancel}
            description={text.cancel}
            disabled={false}
            onActivate={binding.cancel}
          />
        ) : null}
      </NleIconGroup>
      <input
        ref={input}
        type="file"
        accept=".h3proj,application/json"
        hidden
        data-h3-project-open=""
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          if (file !== undefined) void binding.open(file);
        }}
      />
      <p className="h3-meta">{text.privacy}</p>
      <p
        role={state.status === "error" ? "alert" : "status"}
        aria-live="polite"
        data-h3-project-status={state.status}
      >
        {state.error === "relink_outcome_unknown"
          ? text.uncertain
          : state.status === "download"
            ? text.downloadStatus
            : text[state.status]}
        {state.dirty ? ` · ${text.dirty}` : ""}
      </p>
      {state.missingMedia.length === 0 ? null : (
        <fieldset>
          <legend>{text.missing}</legend>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              void binding.refreshRetained?.();
            }}
            title={text.refresh}
            aria-label={text.refresh}
          >
            <NleActionIcon name="refresh" />
          </button>
          {state.missingMedia.map((id) => (
            <div className="h3-project-media-row" key={id}>
              <label>
                <span>{id}</span>
                <select
                  aria-label={`${text.choose}: ${id}`}
                  disabled={busy}
                  value={choices[id] ?? ""}
                  onChange={(event) =>
                    setChoices({ ...choices, [id]: event.target.value })
                  }
                >
                  <option value="">{text.choose}</option>
                  {(binding.retained ?? []).map((row) => (
                    <option key={row.asset_id} value={row.asset_id}>
                      {row.width} × {row.height}, {row.frame_count}f (
                      {row.asset_id.slice(-8)})
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                disabled={busy || !choices[id]}
                aria-label={`${text.relink}: ${id}`}
                title={text.relink}
                onClick={() => {
                  void binding.relink(id, choices[id]);
                }}
              >
                <NleActionIcon name="import" />
              </button>
            </div>
          ))}
        </fieldset>
      )}
      <ProjectRecoveryControls
        binding={binding.recovery}
        locale={locale}
        blocked={busy}
      />
    </div>
  );
  return compact ? (
    <details
      ref={menu}
      className="h3-project-menu"
      onKeyDown={(event) => {
        if (event.key === "Escape" && menu.current?.open) {
          event.stopPropagation();
          menu.current.open = false;
          menu.current.querySelector("summary")?.focus();
        }
      }}
    >
      <summary title={text.project} aria-label={text.project}>
        <NleActionIcon name="project" />
      </summary>
      {body}
    </details>
  ) : (
    body
  );
}
