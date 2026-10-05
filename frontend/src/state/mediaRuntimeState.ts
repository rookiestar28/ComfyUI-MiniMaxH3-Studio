// M25-33: the page-lifecycle state of the shared Media tools entry.
//
// Mount memory only. It holds the last decoded status, the running or last setup job, at most one
// pending continuation and a closed notice. A continuation is never persisted: a reload, a view
// release or leaving its context drops it.

import type {
  MediaRuntimeAction,
  MediaRuntimeFeature,
  MediaRuntimeJob,
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

export type MediaRuntimeNotice =
  | "continued"
  | "select_again"
  | "continuation_cleared"
  | "continuation_replaced"
  | "feature_not_ready"
  | "outcome_unknown";

export type MediaRuntimeResume = Readonly<{
  kind: MediaRuntimeIntentKind;
  token: number;
  /** `intentIdentity` of the continued intent, so exactly the matching surface reacts. */
  identity: string;
}>;

export type MediaRuntimeState = Readonly<{
  status: "unread" | "reading" | "read" | "unavailable";
  wire: MediaRuntimeStatus | null;
  job: MediaRuntimeJob | null;
  pending: MediaRuntimeIntent | null;
  busy: MediaRuntimeAction | null;
  refusal: MediaRuntimeClientCode | null;
  notice: MediaRuntimeNotice | null;
  resume: MediaRuntimeResume | null;
  /** Bumped when focus returns to the card's primary control. */
  focusPrimary: number;
}>;

export const initialMediaRuntimeState: MediaRuntimeState = Object.freeze({
  status: "unread",
  wire: null,
  job: null,
  pending: null,
  busy: null,
  refusal: null,
  notice: null,
  resume: null,
  focusPrimary: 0,
});

/** A stable, content-free key over an intent's captured identity. */
export function intentIdentity(intent: MediaRuntimeIntent): string {
  switch (intent.kind) {
    case "production_import":
      return JSON.stringify([
        intent.kind,
        intent.productionWorkspaceHandle,
        intent.productionWorkspaceId,
        intent.segmentIds,
        intent.outputHandles,
      ]);
    case "clip_preview":
      return JSON.stringify([
        intent.kind,
        intent.workspaceHandle,
        intent.referenceRevision,
        intent.timelineRevision,
        intent.timelineContentFingerprint,
        intent.clipId,
        intent.overlayGeneration,
      ]);
    case "final_render":
      return JSON.stringify([
        intent.kind,
        intent.workspaceHandle,
        intent.workspaceRevision,
        intent.timelineRevision,
        intent.publicFingerprint,
        intent.overlayGeneration,
      ]);
  }
}

/** What every surface receives; the functions are stable for the session's lifetime. */
export type MediaToolsBinding = Readonly<{
  state: MediaRuntimeState;
  ensure(): void;
  report(feature: MediaRuntimeFeature): void;
  install(intent: MediaRuntimeIntent | null): void;
  cancel(): void;
  dismiss(): void;
  rescan(): void;
  reclaim(): void;
  useLocalDirectory(directory: string): void;
  restoreAuto(): void;
}>;
