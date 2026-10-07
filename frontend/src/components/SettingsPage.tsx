import type { LanguageSettingSnapshot } from "../host/languageSettings";
import type {
  ProviderIntent,
  ProviderIntentPayload,
  ProviderIntentResult,
  ProviderSettingsProjection,
} from "../contracts/providerSettingsCodec";
import { ProviderSettingsSection } from "./ProviderSettingsSection";
import type { Locale } from "../i18n/catalog";
import {
  H3_LANGUAGE_SETTING_ID,
  type LocalePreference,
} from "../i18n/localeStore";
import type { SidebarRetention } from "../state/sidebarRetention";
import type { MediaToolsBinding } from "../state/mediaRuntimeState";
import { MediaToolsCard } from "./MediaToolsCard";
import { WorkspaceStateSection } from "./WorkspaceStateSection";
import type { WorkspaceStateBinding } from "../lifecycle/workspaceStateSession";
import type { RetainedAssetsBinding } from "../lifecycle/retainedAssetsSession";
import { RetainedAssetsSection } from "./RetainedAssetsSection";

const copy = {
  en: {
    title: "Settings",
    description:
      "Presentation preferences use the supported ComfyUI setting store and do not alter prompts or workflows.",
    language: "Language",
    auto: "Automatic (ComfyUI locale)",
    en: "English",
    "zh-TW": "繁體中文",
    "zh-CN": "简体中文",
    pending: "Saving language preference.",
    invalid: "The stored language value is invalid.",
    unavailable: "The supported settings storage is unavailable.",
    failed: "The language preference was not committed.",
  },
  "zh-TW": {
    title: "設定",
    description:
      "顯示偏好使用受支援的 ComfyUI 設定儲存，不會修改提示詞或工作流程。",
    language: "語言",
    auto: "自動（ComfyUI 語系）",
    en: "English",
    "zh-TW": "繁體中文",
    "zh-CN": "简体中文",
    pending: "正在儲存語言偏好。",
    invalid: "已儲存的語言值無效。",
    unavailable: "受支援的設定儲存目前不可用。",
    failed: "語言偏好未完成寫入。",
  },
  "zh-CN": {
    title: "设置",
    description:
      "显示偏好使用受支持的 ComfyUI 设置存储，不会修改提示词或工作流。",
    language: "语言",
    auto: "自动（ComfyUI 语言）",
    en: "English",
    "zh-TW": "繁體中文",
    "zh-CN": "简体中文",
    pending: "正在保存语言偏好。",
    invalid: "已保存的语言值无效。",
    unavailable: "受支持的设置存储当前不可用。",
    failed: "语言偏好未完成写入。",
  },
} as const;

export function SettingsPage({
  locale,
  snapshot,
  onWrite,
  providerProjection,
  onProviderIntent,
  providerRejection,
  providerBusy,
  providerBusyIntent,
  onProviderCredentialClearerChange,
  retention,
  mediaTools,
  recoveryMetadata,
  retainedAssets,
}: {
  locale: Locale;
  snapshot: LanguageSettingSnapshot;
  onWrite(value: LocalePreference): void | Promise<boolean>;
  /** M22-06: absent until the host seam answers; the section renders nothing. */
  providerProjection?: ProviderSettingsProjection;
  onProviderIntent?(
    intent: ProviderIntent,
    payload?: ProviderIntentPayload,
  ): void | Promise<ProviderIntentResult | void>;
  providerRejection?: string;
  providerBusy?: boolean;
  providerBusyIntent?: ProviderIntent;
  onProviderCredentialClearerChange?(clearer: (() => void) | undefined): void;
  /** M25-21: section expansion only; nothing credential-bearing is retained. */
  retention?: SidebarRetention;
  /** M25-33: the shared Media tools status and setup; absent when rendered on its own. */
  mediaTools?: MediaToolsBinding;
  recoveryMetadata?: WorkspaceStateBinding;
  retainedAssets?: RetainedAssetsBinding;
}) {
  const text = copy[locale];
  const disabled =
    snapshot.pending || snapshot.status === "setting_storage_unavailable";
  const status = snapshot.pending
    ? text.pending
    : snapshot.status === "setting_value_invalid"
      ? text.invalid
      : snapshot.status === "setting_storage_unavailable"
        ? text.unavailable
        : snapshot.status === "write_failed"
          ? text.failed
          : undefined;
  return (
    <section className="h3s-p" aria-labelledby="h3-settings-title">
      <h2 id="h3-settings-title" className="h3ta">
        {text.title}
      </h2>
      <p className="h3ds">{text.description}</p>
      <div className="h3s-r" data-setting-id={H3_LANGUAGE_SETTING_ID}>
        <label htmlFor="h3-language-setting">{text.language}</label>
        <select
          id="h3-language-setting"
          data-h3-focus-key="settings-language"
          value={snapshot.value ?? ""}
          disabled={disabled}
          aria-invalid={
            snapshot.status === "setting_value_invalid" || undefined
          }
          onChange={(event) =>
            onWrite(event.currentTarget.value as LocalePreference)
          }
        >
          {snapshot.value === undefined ? <option value="">—</option> : null}
          {(["auto", "en", "zh-TW", "zh-CN"] as const).map((value) => (
            <option key={value} value={value}>
              {text[value]}
            </option>
          ))}
        </select>
      </div>
      {status !== undefined ? (
        <p role="status" aria-live="polite">
          {status}
        </p>
      ) : null}
      {mediaTools !== undefined ? (
        <MediaToolsCard
          placement="settings"
          binding={mediaTools}
          locale={locale}
        />
      ) : null}
      {recoveryMetadata !== undefined ? (
        <WorkspaceStateSection locale={locale} binding={recoveryMetadata} />
      ) : null}
      {retainedAssets !== undefined ? (
        <RetainedAssetsSection locale={locale} binding={retainedAssets} />
      ) : null}
      {/* M22-06: provider selection lives in this page. There is no second
          settings surface, and this section is the only place it appears. */}
      <ProviderSettingsSection
        locale={locale}
        projection={providerProjection}
        onIntent={onProviderIntent ?? (() => undefined)}
        rejection={providerRejection}
        busy={providerBusy}
        busyIntent={providerBusyIntent}
        onCredentialClearerChange={onProviderCredentialClearerChange}
        retention={retention}
      />
    </section>
  );
}
