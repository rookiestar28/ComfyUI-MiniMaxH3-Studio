import { useEffect, useRef, useState } from "react";
import type { Locale } from "../i18n/catalog";
import type { WorkspaceRecordState } from "../contracts/workspaceStateCodec";
import type { WorkspaceStateBinding } from "../lifecycle/workspaceStateSession";
import { NleActionIcon, type NleIconName } from "./nle/NleIconActions";

const COPY = {
  en: {
    title: "Recovery metadata",
    enabled: "Keep recovery metadata",
    record: "Recovery record",
    choose: "Select a record",
    empty: "No saved records",
    refresh: "Refresh metadata",
    save: "Save metadata",
    restore: "Restore metadata",
    clear: "Clear metadata",
    confirm: "Confirm clear",
    cancel: "Cancel",
    confirmation: "Clear saved recovery metadata?",
    disabled: "Disabled",
    dirty: "Unsaved changes",
    saved: "Saved",
    blocked: "Recovery blocked",
    loading: "Reading metadata",
    working: "Updating metadata",
    production: "Production",
    authoring: "Authoring",
    managed_run: "Generation",
    segments: "Segments",
    readonly: "Read-only recovery",
    source: "Source authorization required",
    savedAt: "Last saved",
    unavailable: "Recovery unavailable on this host",
    conflict: "Metadata changed",
    capacity: "Recovery capacity reached",
    busy: "Recovery storage is busy",
    unknown: "Save outcome unknown",
    failed: "Metadata operation failed",
    workspace_active: "Active workspace",
    workspace_closed: "Closed workspace",
    created: "Created",
    context_ready: "Context ready",
    production_ready: "Production ready",
    sequence_prepared: "Prepared",
    submitted: "Submitted",
    running: "Running",
    artifact_recorded: "Output recorded",
    terminal_succeeded: "Completed",
    terminal_failed: "Failed",
    terminal_cancelled: "Cancelled",
    terminal_unknown_ownership: "Execution ownership unknown",
    expired: "Expired",
  },
  "zh-TW": {
    title: "復原中繼資料",
    enabled: "保留復原中繼資料",
    record: "復原紀錄",
    choose: "選取紀錄",
    empty: "沒有已儲存的紀錄",
    refresh: "重新整理中繼資料",
    save: "儲存中繼資料",
    restore: "復原中繼資料",
    clear: "清除中繼資料",
    confirm: "確認清除",
    cancel: "取消",
    confirmation: "清除已儲存的復原中繼資料？",
    disabled: "已停用",
    dirty: "有未儲存的變更",
    saved: "已儲存",
    blocked: "復原受阻",
    loading: "正在讀取中繼資料",
    working: "正在更新中繼資料",
    production: "製作",
    authoring: "編輯",
    managed_run: "生成",
    segments: "片段",
    readonly: "唯讀復原",
    source: "需要重新授權來源",
    savedAt: "上次儲存",
    unavailable: "此主機無法使用復原",
    conflict: "中繼資料已變更",
    capacity: "復原容量已達上限",
    busy: "復原儲存使用中",
    unknown: "儲存結果不確定",
    failed: "中繼資料操作失敗",
    workspace_active: "工作區使用中",
    workspace_closed: "工作區已關閉",
    created: "已建立",
    context_ready: "Context 已就緒",
    production_ready: "製作已就緒",
    sequence_prepared: "已準備",
    submitted: "已提交",
    running: "執行中",
    artifact_recorded: "已記錄輸出",
    terminal_succeeded: "已完成",
    terminal_failed: "失敗",
    terminal_cancelled: "已取消",
    terminal_unknown_ownership: "執行所有權不確定",
    expired: "已過期",
  },
  "zh-CN": {
    title: "恢复元数据",
    enabled: "保留恢复元数据",
    record: "恢复记录",
    choose: "选择记录",
    empty: "没有已保存的记录",
    refresh: "刷新元数据",
    save: "保存元数据",
    restore: "恢复元数据",
    clear: "清除元数据",
    confirm: "确认清除",
    cancel: "取消",
    confirmation: "清除已保存的恢复元数据？",
    disabled: "已停用",
    dirty: "有未保存的更改",
    saved: "已保存",
    blocked: "恢复受阻",
    loading: "正在读取元数据",
    working: "正在更新元数据",
    production: "制作",
    authoring: "编辑",
    managed_run: "生成",
    segments: "片段",
    readonly: "只读恢复",
    source: "需要重新授权来源",
    savedAt: "上次保存",
    unavailable: "此主机无法使用恢复",
    conflict: "元数据已更改",
    capacity: "恢复容量已达上限",
    busy: "恢复存储使用中",
    unknown: "保存结果不确定",
    failed: "元数据操作失败",
    workspace_active: "工作区使用中",
    workspace_closed: "工作区已关闭",
    created: "已创建",
    context_ready: "Context 已就绪",
    production_ready: "制作已就绪",
    sequence_prepared: "已准备",
    submitted: "已提交",
    running: "执行中",
    artifact_recorded: "已记录输出",
    terminal_succeeded: "已完成",
    terminal_failed: "失败",
    terminal_cancelled: "已取消",
    terminal_unknown_ownership: "执行所有权不确定",
    expired: "已过期",
  },
} as const;

export function WorkspaceStateSection({
  locale,
  binding,
}: {
  locale: Locale;
  binding: WorkspaceStateBinding;
}) {
  const text = COPY[locale],
    { state } = binding,
    projection = state.projection;
  const [confirming, setConfirming] = useState(false);
  const clearRef = useRef<HTMLButtonElement>(null),
    cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    binding.ensure();
    return binding.leave;
  }, [binding.ensure, binding.leave]);
  useEffect(() => {
    if (confirming) cancelRef.current?.focus();
  }, [confirming]);
  const busy = state.busy !== null,
    mutable = state.confirmed && projection?.supported === true && !busy;
  const error =
    state.error === null
      ? null
      : state.error === "outcome_unknown"
        ? text.unknown
        : state.error === "revision_conflict"
          ? text.conflict
          : state.error.startsWith("quota_") || state.error === "record_bound"
            ? text.capacity
            : [
                  "host_unqualified",
                  "filesystem_unqualified",
                  "owner_changed",
                  "owner_mismatch",
                  "storage_unsafe",
                ].includes(state.error)
              ? text.unavailable
              : ["owner_busy", "catalog_busy"].includes(state.error)
                ? text.busy
                : text.failed;
  const cancel = () => {
    setConfirming(false);
    clearRef.current?.focus();
  };
  const action = (
    key: string,
    icon: NleIconName,
    label: string,
    disabled: boolean,
    activate: () => void,
  ) => (
    <button
      type="button"
      className="h3-icon-button"
      data-hue="info"
      data-h3-focus-key={`settings-recovery-${key}`}
      title={label}
      aria-label={label}
      aria-disabled={disabled || undefined}
      onClick={() => {
        if (!disabled) activate();
      }}
    >
      <NleActionIcon name={icon} />
    </button>
  );
  const recovered = state.recovered;
  return (
    <section
      className="h3s-recovery"
      aria-labelledby="h3-recovery-title"
      data-h3-recovery="true"
    >
      <h3 id="h3-recovery-title">{text.title}</h3>
      <label className="h3s-recovery-toggle">
        <input
          type="checkbox"
          data-h3-focus-key="settings-recovery-enable"
          checked={projection?.enabled ?? false}
          disabled={!mutable}
          onChange={(event) =>
            void binding.setEnabled(event.currentTarget.checked)
          }
        />
        {text.enabled}
      </label>
      <p role="status" aria-live="polite" className="h3s-recovery-state">
        {busy
          ? state.busy === "status"
            ? text.loading
            : text.working
          : (error ??
            (projection === null
              ? text.unavailable
              : text[projection.save_state]))}
      </p>
      {projection?.saved_at_ms !== null &&
      projection?.saved_at_ms !== undefined ? (
        <p className="h3-meta">
          {text.savedAt}:{" "}
          <time dateTime={new Date(projection.saved_at_ms).toISOString()}>
            {new Date(projection.saved_at_ms).toLocaleString(locale)}
          </time>
        </p>
      ) : null}
      <div
        className="h3s-recovery-actions"
        role="group"
        aria-label={text.title}
      >
        {action(
          "refresh",
          "refresh",
          text.refresh,
          busy,
          () => void binding.refresh(),
        )}
        {action(
          "save",
          "export",
          text.save,
          !mutable || !projection?.enabled,
          () => void binding.save(),
        )}
        {action(
          "restore",
          "import",
          text.restore,
          !mutable || !projection?.enabled || state.selected === null,
          () => void binding.restore(),
        )}
        <button
          ref={clearRef}
          type="button"
          className="h3-icon-button"
          data-hue="audit"
          data-h3-focus-key="settings-recovery-clear"
          title={text.clear}
          aria-label={text.clear}
          aria-disabled={!mutable || undefined}
          onClick={() => {
            if (mutable) setConfirming(true);
          }}
        >
          <NleActionIcon name="delete" />
        </button>
      </div>
      {confirming ? (
        <fieldset className="h3s-recovery-confirm">
          <legend>{text.confirmation}</legend>
          <button
            type="button"
            data-h3-focus-key="settings-recovery-confirm"
            disabled={busy}
            onClick={() => {
              void binding.reset().then(cancel);
            }}
          >
            {text.confirm}
          </button>
          <button
            ref={cancelRef}
            type="button"
            data-h3-focus-key="settings-recovery-cancel"
            disabled={busy}
            onClick={cancel}
          >
            {text.cancel}
          </button>
        </fieldset>
      ) : null}
      <div className="h3s-r">
        <label htmlFor="h3-recovery-record">{text.record}</label>
        <select
          id="h3-recovery-record"
          data-h3-focus-key="settings-recovery-record"
          value={state.selected ?? ""}
          disabled={!mutable || !projection?.enabled || projection.count === 0}
          onChange={(event) =>
            binding.select(event.currentTarget.value || null)
          }
        >
          <option value="">
            {projection?.count ? text.choose : text.empty}
          </option>
          {projection?.records.map((row, index) => (
            <option key={row.record_id} value={row.record_id}>
              {text[row.kind]} {index + 1} /{" "}
              {text[row.state as WorkspaceRecordState]}
            </option>
          ))}
        </select>
      </div>
      {projection?.count === 0 ? <p className="h3-meta">{text.empty}</p> : null}
      {recovered !== null ? (
        <div className="h3s-recovery-result" role="status" aria-live="polite">
          <h4>{text.readonly}</h4>
          <p>
            {text[recovered.kind]} / {text[recovered.state]}
          </p>
          <p>
            {text.segments}: {recovered.segment_count}
          </p>
          <p>{text.source}</p>
        </div>
      ) : null}
    </section>
  );
}
