// the page-lifecycle state of the shared Media tools entry.
//
// Mount memory only. It holds the last decoded status, the running or last setup job, at most one
// pending continuation and a closed notice. A continuation is never persisted: a reload, a view
// release or leaving its context drops it.

import type {
  MediaRuntimeAction,
  MediaRuntimeFeature,
  MediaRuntimeStatus,
} from "../contracts/mediaRuntimeCodec";
import type { MediaRuntimeClientCode } from "../host/mediaRuntimeClient";

/** The user action a setup continues. Every captured value is revalidated before it runs. */
export type MediaRuntimeIntent =
  | Readonly<{
      kind: "production_import";
      productionWorkspaceHandle: string;
      productionWorkspaceId: string;
      segmentIds: readonly string[];
      outputHandles: readonly string[];
    }>
  | Readonly<{
      kind: "clip_preview";
      workspaceHandle: string;
      referenceRevision: number;
      timelineRevision: number;
      timelineContentFingerprint: string;
      clipId: string;
      /** The overlay generation for the expanded editor's monitor; `null` in the sidebar. */
      overlayGeneration: number | null;
    }>
  | Readonly<{
      kind: "final_render";
      workspaceHandle: string;
      workspaceRevision: number;
      timelineRevision: number;
      publicFingerprint: string;
      overlayGeneration: number;
    }>;

export type MediaRuntimeIntentKind = MediaRuntimeIntent["kind"];

export const MEDIA_RUNTIME_INTENT_FEATURE: Readonly<
  Record<MediaRuntimeIntentKind, MediaRuntimeFeature>
> = Object.freeze({
  production_import: "import",
  clip_preview: "preview",
  final_render: "render",
});

export type MediaRuntimeNotice = "retry_action";

export type MediaRuntimeState = Readonly<{
  status: "unread" | "reading" | "read" | "unavailable";
  wire: MediaRuntimeStatus | null;
  busy: MediaRuntimeAction | null;
  refusal: MediaRuntimeClientCode | null;
  notice: MediaRuntimeNotice | null;
  /** Bumped when focus returns to the card's primary control. */
  focusPrimary: number;
}>;

export const initialMediaRuntimeState: MediaRuntimeState = Object.freeze({
  status: "unread",
  wire: null,
  busy: null,
  refusal: null,
  notice: null,
  focusPrimary: 0,
});

/** What every surface receives; the functions are stable for the session's lifetime. */
export type MediaToolsBinding = Readonly<{
  state: MediaRuntimeState;
  ensure(): void;
  report(feature: MediaRuntimeFeature): void;
  dismiss(): void;
  rescan(): void;
  reclaim(): void;
  useLocalDirectory(directory: string): void;
  restoreAuto(): void;
}>;
