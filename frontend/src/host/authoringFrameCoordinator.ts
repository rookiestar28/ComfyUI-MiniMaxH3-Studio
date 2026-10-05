import {
  AUTHORING_PREVIEW_REQUEST_SCHEMA,
  type AuthoringPreviewRequest,
} from "../contracts/authoringPreviewCodec";
import type { AuthoringMediaPreviewAudioDisposition } from "./authoringMediaPreview";

export type AuthoringPreviewIdentity = Readonly<{
  workspaceHandle: string;
  referenceRevision: number;
  timelineRevision: number;
  timelineContentFingerprint: string;
  clipId: string;
  startFrame: number;
  frames: number;
  sourceStartFrame: number;
  fps: number;
}>;

export type AuthoringPreviewTransportState =
  | "idle"
  | "loading"
  | "ready"
  | "paused"
  | "playing"
  | "seeking"
  | "unavailable"
  | "failed";

export type AuthoringFrameSnapshot = Readonly<{
  status: AuthoringPreviewTransportState;
  clipId?: string;
  playheadFrame?: number;
  clipLocalFrame?: number;
  sourceFrame?: number;
  playbackActive?: boolean;
  objectUrl?: string;
  audioDisposition?: AuthoringMediaPreviewAudioDisposition;
  reason?: string;
  warmStatus?: "idle" | "loading" | "ready" | "unavailable";
  warmClipId?: string;
  warmObjectUrl?: string;
  warmAudioDisposition?: AuthoringMediaPreviewAudioDisposition;
  warmReason?: string;
}>;

export type AuthoringPreviewOpener = (
  request: AuthoringPreviewRequest,
  signal: AbortSignal,
) => Promise<
  Readonly<{
    blob: Blob;
    audioDisposition: AuthoringMediaPreviewAudioDisposition;
  }>
>;

function exactIdentity(identity: AuthoringPreviewIdentity): void {
  if (
    !Number.isInteger(identity.startFrame) ||
    identity.startFrame < 0 ||
    !Number.isInteger(identity.frames) ||
    identity.frames < 1 ||
    !Number.isInteger(identity.sourceStartFrame) ||
    identity.sourceStartFrame < 0 ||
    !Number.isInteger(identity.fps) ||
    identity.fps < 1
  )
    throw new Error("authoring preview identity is invalid");
}

export function previewSecondsForTimelineFrame(
  identity: AuthoringPreviewIdentity,
  timelineFrame: number,
): number {
  exactIdentity(identity);
  if (
    !Number.isInteger(timelineFrame) ||
    timelineFrame < identity.startFrame ||
    timelineFrame >= identity.startFrame + identity.frames
  )
    throw new Error("timeline frame is outside the half-open clip interval");
  return (timelineFrame - identity.startFrame) / identity.fps;
}

export function observePreviewSeconds(
  identity: AuthoringPreviewIdentity,
  previewSeconds: number,
) {
  exactIdentity(identity);
  if (!Number.isFinite(previewSeconds))
    throw new Error("preview seconds must be finite");
  // IMPORTANT: round exactly once at the normalized media-time boundary. Reintroducing the
  // source offset before rounding shifts every trimmed clip and breaks M25-02's zero-based Blob.
  const local = Math.min(
    identity.frames - 1,
    Math.max(0, Math.floor(previewSeconds * identity.fps + 0.5)),
  );
  return Object.freeze({
    timelineFrame: identity.startFrame + local,
    clipLocalFrame: local,
    sourceFrame: identity.sourceStartFrame + local,
  });
}

function failureReason(error: unknown): Readonly<{
  status: "unavailable" | "failed";
  reason: string;
}> {
  const disposition =
    error !== null && typeof error === "object" && "disposition" in error
      ? (error as { disposition?: unknown }).disposition
      : undefined;
  if (
    disposition === "unavailable" ||
    disposition === "stale" ||
    disposition === "unsupported" ||
    disposition === "too_large" ||
    disposition === "too_long" ||
    disposition === "busy" ||
    disposition === "cancelled" ||
    disposition === "timeout"
  )
    return Object.freeze({ status: "unavailable", reason: disposition });
  return Object.freeze({ status: "failed", reason: "failed" });
}

export function createAuthoringFrameCoordinator({
  openPreview,
  createObjectURL,
  revokeObjectURL,
  changed,
}: {
  openPreview: AuthoringPreviewOpener;
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
  changed(): void;
}) {
  let generation = 0;
  let activeController: AbortController | undefined;
  let warmController: AbortController | undefined;
  let identity: AuthoringPreviewIdentity | undefined;
  let warmIdentity: AuthoringPreviewIdentity | undefined;
  let activeObjectUrl: string | undefined;
  let warmObjectUrl: string | undefined;
  let state: AuthoringFrameSnapshot = Object.freeze({ status: "idle" });

  const publish = (next: AuthoringFrameSnapshot) => {
    state = Object.freeze(next);
    changed();
  };
  const clearOwned = () => {
    activeController?.abort();
    warmController?.abort();
    activeController = undefined;
    warmController = undefined;
    if (activeObjectUrl !== undefined) revokeObjectURL(activeObjectUrl);
    if (warmObjectUrl !== undefined) revokeObjectURL(warmObjectUrl);
    activeObjectUrl = undefined;
    warmObjectUrl = undefined;
    warmIdentity = undefined;
  };

  const requestFor = (
    next: AuthoringPreviewIdentity,
    requestId: string,
  ): AuthoringPreviewRequest =>
    Object.freeze({
      schema: AUTHORING_PREVIEW_REQUEST_SCHEMA,
      requestId,
      workspaceHandle: next.workspaceHandle,
      referenceRevision: next.referenceRevision,
      timelineRevision: next.timelineRevision,
      timelineContentFingerprint: next.timelineContentFingerprint,
      clipId: next.clipId,
    });

  return Object.freeze({
    snapshot: () => state,
    async open(
      next: AuthoringPreviewIdentity,
      warm?: AuthoringPreviewIdentity,
    ): Promise<void> {
      exactIdentity(next);
      if (warm !== undefined) exactIdentity(warm);
      generation += 1;
      const owned = generation;
      clearOwned();
      identity = Object.freeze({ ...next });
      const requestController = new AbortController();
      activeController = requestController;
      publish({
        status: "loading",
        clipId: next.clipId,
        playheadFrame: next.startFrame,
        clipLocalFrame: 0,
        sourceFrame: next.sourceStartFrame,
        playbackActive: false,
        warmStatus: "idle",
      });
      try {
        const result = await openPreview(
          requestFor(next, `authoring-preview-${owned}`),
          requestController.signal,
        );
        // Keep the request-local signal: a replacement clears the shared owner before this await resumes.
        if (owned !== generation || requestController.signal.aborted) return;
        const objectUrl = createObjectURL(result.blob);
        if (owned !== generation || requestController.signal.aborted) {
          revokeObjectURL(objectUrl);
          return;
        }
        activeObjectUrl = objectUrl;
        publish({
          status: "ready",
          clipId: next.clipId,
          playheadFrame: next.startFrame,
          clipLocalFrame: 0,
          sourceFrame: next.sourceStartFrame,
          playbackActive: false,
          objectUrl,
          audioDisposition: result.audioDisposition,
          warmStatus: "idle",
        });
      } catch (error) {
        if (owned !== generation || requestController.signal.aborted) return;
        activeController = undefined;
        publish({
          ...failureReason(error),
          clipId: next.clipId,
          playheadFrame: next.startFrame,
          clipLocalFrame: 0,
          sourceFrame: next.sourceStartFrame,
          playbackActive: false,
          warmStatus: "idle",
        });
        return;
      }

      if (warm === undefined || owned !== generation) return;
      const candidate = Object.freeze({ ...warm });
      warmIdentity = candidate;
      const candidateController = new AbortController();
      warmController = candidateController;
      publish({
        ...state,
        warmStatus: "loading",
        warmClipId: candidate.clipId,
      });
      try {
        const result = await openPreview(
          requestFor(candidate, `authoring-preview-${owned}-warm`),
          candidateController.signal,
        );
        if (owned !== generation || candidateController.signal.aborted) return;
        const objectUrl = createObjectURL(result.blob);
        if (owned !== generation || candidateController.signal.aborted) {
          revokeObjectURL(objectUrl);
          return;
        }
        warmObjectUrl = objectUrl;
        publish({
          ...state,
          warmStatus: "ready",
          warmClipId: candidate.clipId,
          warmObjectUrl: objectUrl,
          warmAudioDisposition: result.audioDisposition,
        });
      } catch (error) {
        if (owned !== generation || candidateController.signal.aborted) return;
        warmController = undefined;
        publish({
          ...state,
          warmStatus: "unavailable",
          warmClipId: candidate.clipId,
          warmReason: failureReason(error).reason,
        });
      }
    },
    promoteWarm(): boolean {
      if (
        warmIdentity === undefined ||
        warmObjectUrl === undefined ||
        state.warmStatus !== "ready"
      )
        return false;
      const promotedIdentity = warmIdentity;
      const promotedUrl = warmObjectUrl;
      const promotedAudio = state.warmAudioDisposition;
      const previousUrl = activeObjectUrl;
      const continuePlayback = state.playbackActive === true;
      identity = promotedIdentity;
      activeObjectUrl = promotedUrl;
      warmIdentity = undefined;
      warmObjectUrl = undefined;
      if (previousUrl !== undefined) revokeObjectURL(previousUrl);
      publish({
        status: "ready",
        clipId: promotedIdentity.clipId,
        playheadFrame: promotedIdentity.startFrame,
        clipLocalFrame: 0,
        sourceFrame: promotedIdentity.sourceStartFrame,
        playbackActive: continuePlayback,
        objectUrl: promotedUrl,
        audioDisposition: promotedAudio,
        warmStatus: "idle",
      });
      return true;
    },
    observe(seconds: number, status: "paused" | "playing" | "seeking") {
      if (identity === undefined || state.objectUrl === undefined) return;
      const observed = observePreviewSeconds(identity, seconds);
      publish({
        ...state,
        status,
        playheadFrame: observed.timelineFrame,
        clipLocalFrame: observed.clipLocalFrame,
        sourceFrame: observed.sourceFrame,
        playbackActive:
          status === "playing"
            ? true
            : status === "paused"
              ? false
              : state.playbackActive,
      });
    },
    fail() {
      if (identity === undefined) return;
      clearOwned();
      publish({
        status: "failed",
        reason: "failed",
        clipId: identity.clipId,
        playheadFrame: identity.startFrame,
        clipLocalFrame: 0,
        sourceFrame: identity.sourceStartFrame,
        playbackActive: false,
      });
    },
    unavailable(reason: "play_rejected" | "unsupported") {
      if (identity === undefined) return;
      clearOwned();
      publish({
        status: "unavailable",
        reason,
        clipId: identity.clipId,
        playheadFrame: identity.startFrame,
        clipLocalFrame: 0,
        sourceFrame: identity.sourceStartFrame,
        playbackActive: false,
      });
    },
    close() {
      generation += 1;
      clearOwned();
      identity = undefined;
      if (state.status !== "idle") publish({ status: "idle" });
    },
  });
}
