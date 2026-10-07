export const SIDEBAR_FOCUS_KEYS = [
  "page-context",
  "page-production",
  "page-settings",
  "production-create",
  "production-add",
  "production-refetch",
  "production-release",
  "settings-language",
  "settings-recovery-enable",
  "settings-recovery-refresh",
  "settings-recovery-save",
  "settings-recovery-restore",
  "settings-recovery-clear",
  "settings-recovery-confirm",
  "settings-recovery-cancel",
  "settings-recovery-record",
  "settings-retained-enable",
  "settings-retained-refresh",
  "settings-retained-restore",
  "settings-retained-preview",
  "settings-retained-release",
  "settings-retained-collect",
  "settings-retained-clear",
  "settings-retained-confirm",
  "settings-retained-cancel",
  "settings-retained-asset",
  "production-retain-output",
  "production-retained-refresh",
  "settings-provider",
  // M25-33: the Media tools card's status line in Settings and its one primary control.
  "settings-media-tools",
  "media-tools-primary",
  "app-stage-intent",
  "app-stage-media",
  "app-stage-understand",
  "app-stage-audit",
  "app-stage-execute",
  "app-task-mode",
  "app-first-frame",
  "app-last-frame",
  "app-reference-image",
  "app-reference-video",
  "app-reference-audio",
  "app-intent",
  "app-frame-count",
  "app-submit",
  "app-cancel",
  "edit-app-mode-setup",
  "cancel-app-mode-edit",
  "app-keep-canvas",
  "app-native-nodes",
  "app-stage-action",
  "error-recovery",
  "workspace-stage-intent",
  "workspace-stage-media",
  "workspace-stage-understand",
  "workspace-stage-audit",
  "workspace-stage-execute",
  "workspace-prompt",
  "workspace-reference",
  "workspace-reason",
  "workspace-stage-revision",
  "workspace-validate",
  "workspace-export",
  "workspace-copy",
  "workspace-import",
  // M25-16: the full-editor launcher inside `clip_editor` and the overlay's own close control.
  "nle-open-overlay",
  "nle-close-overlay",
] as const;

export type SidebarFocusKey = (typeof SIDEBAR_FOCUS_KEYS)[number];

const sidebarFocusKeySet = new Set<string>(SIDEBAR_FOCUS_KEYS);

export function isSidebarFocusKey(value: unknown): value is SidebarFocusKey {
  return (
    typeof value === "string" &&
    value.length <= 64 &&
    sidebarFocusKeySet.has(value)
  );
}
