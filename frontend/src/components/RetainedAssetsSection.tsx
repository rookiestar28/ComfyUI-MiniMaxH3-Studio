import { useEffect, useRef, useState } from "react";
import type { Locale } from "../i18n/catalog";
import type { RetainedAssetsBinding } from "../lifecycle/retainedAssetsSession";
import type { ProductionWorkbenchProjection } from "../contracts/productionWorkbenchCodec";
import {
  retainSelectionKey,
  type RetainSelection,
} from "../contracts/retainedAssetsCodec";
import { NleActionIcon, type NleIconName } from "./nle/NleIconActions";

const COPY = {
  en: {
    title: "Retained media",
    enabled: "Keep verified media",
    disabled: "Retention disabled",
    ready: "Retention enabled",
    loading: "Reading retained media",
    working: "Updating retained media",
    unavailable: "Media retention unavailable on this host",
    unknown: "Operation outcome unknown",
    failed: "Retained media operation failed",
    conflict: "Retained media changed",
    capacity: "Retention capacity reached",
    busy: "Media retention is busy",
    relink: "Relink required",
    fresh: "Restored for this session",
    noPreview: "Preview unavailable",
    choose: "Select retained media",
    empty: "No retained media",
    asset: "Retained asset",
    media: "Media",
    expired: "Expired",
    referenced: "Referenced",
    used: "Owner storage",
    protected: "Protected",
    remnants: "Unverified remnants",
    removed: "Removed",
    refresh: "Refresh retained media",
    restore: "Restore retained media",
    preview: "Preview retained media",
    release: "Release restored media",
    clear: "Clear retained media",
    collect: "Remove expired media",
    confirmation: "Clear unreferenced, unused retained media?",
    confirm: "Confirm clear",
    cancel: "Cancel",
    retain: "Retain output",
    retained: "Output retained",
    policy: "Unreferenced retention",
    days: "7 days",
  },
  "zh-TW": {
    title: "保留媒體",
    enabled: "保留已驗證媒體",
    disabled: "保留功能已停用",
    ready: "保留功能已啟用",
    loading: "正在讀取保留媒體",
    working: "正在更新保留媒體",
    unavailable: "此主機無法保留媒體",
    unknown: "操作結果不確定",
    failed: "保留媒體操作失敗",
    conflict: "保留媒體已變更",
    capacity: "保留容量已達上限",
    busy: "保留媒體使用中",
    relink: "需要重新連結",
    fresh: "已復原至本次工作階段",
    noPreview: "無法預覽",
    choose: "選取保留媒體",
    empty: "沒有保留媒體",
    asset: "保留資產",
    media: "媒體",
    expired: "已過期",
    referenced: "已引用",
    used: "目前使用者儲存量",
    protected: "受保護",
    remnants: "未驗證殘留",
    removed: "已移除",
    refresh: "重新整理保留媒體",
    restore: "復原保留媒體",
    preview: "預覽保留媒體",
    release: "釋放已復原媒體",
    clear: "清除保留媒體",
    collect: "移除過期媒體",
    confirmation: "清除未引用且未使用的保留媒體？",
    confirm: "確認清除",
    cancel: "取消",
    retain: "保留輸出",
    retained: "輸出已保留",
    policy: "未引用保留期間",
    days: "7 天",
  },
  "zh-CN": {
    title: "保留媒体",
    enabled: "保留已验证媒体",
    disabled: "保留功能已停用",
    ready: "保留功能已启用",
    loading: "正在读取保留媒体",
    working: "正在更新保留媒体",
    unavailable: "此主机无法保留媒体",
    unknown: "操作结果不确定",
    failed: "保留媒体操作失败",
    conflict: "保留媒体已更改",
    capacity: "保留容量已达上限",
    busy: "保留媒体使用中",
    relink: "需要重新链接",
    fresh: "已恢复至本次会话",
    noPreview: "无法预览",
    choose: "选择保留媒体",
    empty: "没有保留媒体",
    asset: "保留资产",
    media: "媒体",
    expired: "已过期",
    referenced: "已引用",
    used: "当前用户存储量",
    protected: "受保护",
    remnants: "未验证残留",
    removed: "已移除",
    refresh: "刷新保留媒体",
    restore: "恢复保留媒体",
    preview: "预览保留媒体",
    release: "释放已恢复媒体",
    clear: "清除保留媒体",
    collect: "移除过期媒体",
    confirmation: "清除未引用且未使用的保留媒体？",
    confirm: "确认清除",
    cancel: "取消",
    retain: "保留输出",
    retained: "输出已保留",
    policy: "未引用保留期限",
    days: "7 天",
  },
} as const;
function errorText(
  text: (typeof COPY)[Locale],
  error: RetainedAssetsBinding["state"]["error"],
): string | null {
  if (error === null) return null;
  if (error === "outcome_unknown") return text.unknown;
  if (
    [
      "asset_changed",
      "asset_unavailable",
      "source_stale",
      "source_unavailable",
      "lease_invalid",
      "media_unqualified",
    ].includes(error)
  )
    return text.relink;
  if (
    [
      "host_unqualified",
      "filesystem_unqualified",
      "owner_changed",
      "owner_mismatch",
      "storage_unsafe",
    ].includes(error)
  )
    return text.unavailable;
  if (error === "revision_conflict") return text.conflict;
  if (error.startsWith("quota_") || error === "lease_bound")
    return text.capacity;
  if (["owner_busy", "catalog_busy", "work_busy"].includes(error))
    return text.busy;
  return text.failed;
}
function IconAction({
  name,
  icon,
  disabled,
  focusKey,
  run,
}: {
  name: string;
  icon: NleIconName;
  disabled: boolean;
  focusKey: string;
  run(): void;
}) {
  return (
    <button
      type="button"
      className="h3-icon-button"
      data-hue="info"
      title={name}
      aria-label={name}
      aria-disabled={disabled || undefined}
      data-h3-focus-key={focusKey}
      onClick={() => {
        if (!disabled) run();
      }}
    >
      <NleActionIcon name={icon} />
    </button>
  );
}
export function RetainedAssetsSection({
  locale,
  binding,
}: {
  locale: Locale;
  binding: RetainedAssetsBinding;
}) {
  const text = COPY[locale],
    { state } = binding,
    projection = state.projection;
  const [confirming, setConfirming] = useState(false);
  const opener = useRef<HTMLButtonElement>(null),
    cancelButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    binding.ensure();
    return binding.leave;
  }, [binding.ensure, binding.leave]);
  useEffect(() => {
    if (confirming) cancelButton.current?.focus();
  }, [confirming]);
  const busy = state.busy !== null,
    mutable = state.confirmed && projection?.supported === true && !busy;
  const cancel = () => {
    setConfirming(false);
    opener.current?.focus();
  };
  const message = errorText(text, state.error);
  return (
    <section
      className="h3s-recovery"
      data-h3-retained-assets="true"
      aria-labelledby="h3-retained-title"
    >
      <h3 id="h3-retained-title">{text.title}</h3>
      <label className="h3s-recovery-toggle">
        <input
          type="checkbox"
          checked={projection?.enabled ?? false}
          disabled={!mutable}
          data-h3-focus-key="settings-retained-enable"
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
          : (message ??
            (projection?.supported
              ? projection.enabled
                ? text.ready
                : text.disabled
              : text.unavailable))}
      </p>
      {projection !== null ? (
        <div className="h3-meta h3-retained-inventory">
          <p>
            {text.used}:{" "}
            {(projection.charged_bytes / (1024 * 1024)).toLocaleString(locale, {
              maximumFractionDigits: 2,
            })}{" "}
            / 256 MiB
          </p>
          <p>
            {text.asset}: {projection.count} / 16
          </p>
          <p>
            {text.policy}: {text.days}
          </p>
          {projection.remnant_count > 0 ? (
            <p>
              {text.remnants}: {projection.remnant_count}
            </p>
          ) : null}
        </div>
      ) : null}
      <div
        className="h3s-recovery-actions"
        role="group"
        aria-label={text.title}
      >
        <IconAction
          name={text.refresh}
          icon="refresh"
          disabled={busy}
          focusKey="settings-retained-refresh"
          run={() => void binding.refresh()}
        />
        <IconAction
          name={text.restore}
          icon="import"
          disabled={
            !mutable ||
            !projection?.enabled ||
            state.selected === null ||
            state.restored !== null
          }
          focusKey="settings-retained-restore"
          run={() => void binding.restore()}
        />
        <IconAction
          name={text.preview}
          icon="preview"
          disabled={!mutable || !state.restored?.preview_available}
          focusKey="settings-retained-preview"
          run={() => void binding.preview()}
        />
        <IconAction
          name={text.release}
          icon="release"
          disabled={state.restored === null}
          focusKey="settings-retained-release"
          run={() => void binding.releaseUse()}
        />
        <IconAction
          name={text.collect}
          icon="delete"
          disabled={!mutable}
          focusKey="settings-retained-collect"
          run={() => void binding.collect()}
        />
        <button
          ref={opener}
          type="button"
          className="h3-icon-button"
          data-hue="audit"
          title={text.clear}
          aria-label={text.clear}
          aria-disabled={!mutable || undefined}
          data-h3-focus-key="settings-retained-clear"
          onClick={() => {
            if (mutable) setConfirming(true);
          }}
        >
          <NleActionIcon name="reset" />
        </button>
      </div>
      {confirming ? (
        <fieldset className="h3s-recovery-confirm">
          <legend>{text.confirmation}</legend>
          <button
            type="button"
            disabled={!mutable}
            data-h3-focus-key="settings-retained-confirm"
            onClick={() => void binding.clear().then(cancel)}
          >
            {text.confirm}
          </button>
          <button
            ref={cancelButton}
            type="button"
            disabled={busy}
            data-h3-focus-key="settings-retained-cancel"
            onClick={cancel}
          >
            {text.cancel}
          </button>
        </fieldset>
      ) : null}
      <div className="h3s-r">
        <label htmlFor="h3-retained-asset">{text.asset}</label>
        <select
          id="h3-retained-asset"
          data-h3-focus-key="settings-retained-asset"
          value={state.selected ?? ""}
          disabled={!mutable || !projection?.enabled || projection.count === 0}
          onChange={(event) =>
            binding.select(event.currentTarget.value || null)
          }
        >
          <option value="">
            {projection?.count ? text.choose : text.empty}
          </option>
          {projection?.assets.map((row, index) => (
            <option
              key={row.asset_id}
              value={row.asset_id}
              disabled={row.state === "expired"}
            >
              {text.media} {index + 1} / {row.width} x {row.height} /{" "}
              {(row.duration_ms / 1000).toLocaleString(locale)} s
              {row.state === "expired"
                ? ` / ${text.expired}`
                : row.referenced
                  ? ` / ${text.referenced}`
                  : ""}
            </option>
          ))}
        </select>
      </div>
      {state.cleanup !== null ? (
        <p role="status">
          {text.removed}: {state.cleanup.removed} / {text.protected}:{" "}
          {state.cleanup.protected}
        </p>
      ) : projection?.protected ? (
        <p className="h3-meta">
          {text.protected}: {projection.protected}
        </p>
      ) : null}
      {state.restored !== null ? (
        <div className="h3s-recovery-result">
          <p>{text.fresh}</p>
          {!state.restored.preview_available ? <p>{text.noPreview}</p> : null}
        </div>
      ) : null}
      {state.previewUrl !== null && state.restored !== null ? (
        <video
          className="h3-retained-preview"
          src={state.previewUrl}
          controls
          preload="none"
          aria-label={text.preview}
          style={{
            aspectRatio: `${state.restored.width} / ${state.restored.height}`,
          }}
        />
      ) : null}
    </section>
  );
}
export function RetainOutputAction({
  locale,
  binding,
  projection,
  eligible,
}: {
  locale: Locale;
  binding: RetainedAssetsBinding;
  projection: ProductionWorkbenchProjection;
  eligible: boolean;
}) {
  const text = COPY[locale],
    { state } = binding;
  useEffect(() => {
    binding.ensure();
    return binding.leave;
  }, [binding.ensure, binding.leave]);
  const selected =
    projection.selectedSegmentIds.length === 1
      ? projection.selectedSegmentIds[0]
      : null;
  const outputs = projection.outputs.filter(
    (output) =>
      output.segmentId === selected &&
      output.segmentId !== null &&
      output.state === "ready",
  );
  const selection: RetainSelection | null =
    selected !== null && selected !== undefined && outputs.length === 1
      ? {
          workspace_handle: projection.workspaceHandle,
          workspace_id: projection.workspaceId,
          expected_workspace_revision: projection.workspaceRevision,
          expected_workspace_fingerprint: projection.workspaceFingerprint,
          segment_id: selected,
          output_handle: outputs[0]!.outputHandle,
        }
      : null;
  const mutable =
    eligible &&
    selection !== null &&
    state.confirmed &&
    state.projection?.enabled === true &&
    state.busy === null;
  const retained =
    selection !== null &&
    state.retainedId !== null &&
    state.retainedKey === retainSelectionKey(selection);
  return (
    <section
      className="h3s-recovery h3-retain-output"
      data-h3-retain-output="true"
      aria-label={text.title}
    >
      <div className="h3s-recovery-actions">
        <IconAction
          name={text.retain}
          icon="keep"
          disabled={!mutable || retained}
          focusKey="production-retain-output"
          run={() => {
            if (selection !== null) void binding.retain(selection);
          }}
        />
        <IconAction
          name={text.refresh}
          icon="refresh"
          disabled={state.busy !== null}
          focusKey="production-retained-refresh"
          run={() => void binding.refresh()}
        />
        <p role="status" aria-live="polite">
          {state.busy !== null
            ? text.working
            : (errorText(text, state.error) ??
              (retained
                ? text.retained
                : state.projection?.enabled
                  ? text.ready
                  : text.disabled))}
        </p>
      </div>
    </section>
  );
}
