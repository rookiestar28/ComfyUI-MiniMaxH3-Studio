// Manual selection session. Runtime readiness never retries a media action for the user.

import type {
  MediaRuntimeAction,
  MediaRuntimeFeature,
  MediaRuntimeStatus,
} from "../contracts/mediaRuntimeCodec";
import type {
  MediaRuntimeActionPayload,
  MediaRuntimeStatusResult,
} from "../host/mediaRuntimeClient";
import {
  initialMediaRuntimeState,
  type MediaRuntimeState,
  type MediaToolsBinding,
} from "../state/mediaRuntimeState";
import type { ShellRuntime } from "./shellSession";

export const MEDIA_RUNTIME_POLL_INTERVAL_MS = 1_000;
/** Status re-reads while a newly selected feature is still activating. */
export const MEDIA_RUNTIME_MAX_ACTIVATION_READS = 10;

export function createMediaRuntimeSession(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;
  // IMPORTANT: release advances generation; late reads or mutations cannot repopulate a released view.
  let generation = 0;
  let readAbort: AbortController | undefined;
  /** The status each feature's report last read, so a remount does not read it again. */
  const explained = new Map<MediaRuntimeFeature, MediaRuntimeStatus | null>();

  const state = (): MediaRuntimeState => session.mediaRuntime;

  function patch(next: Partial<MediaRuntimeState>): void {
    session.mediaRuntime = Object.freeze({ ...session.mediaRuntime, ...next });
    actions.renderCurrent();
  }

  function focusPrimary(): number {
    return state().focusPrimary + 1;
  }

  // ------------------------------------------------------------------ reads

  async function readStatus(): Promise<MediaRuntimeStatus | null> {
    const owned = generation;
    readAbort?.abort();
    const controller = new AbortController();
    readAbort = controller;
    patch({ status: "reading" });
    const result = await deps.mediaRuntimeClient.readStatus(controller.signal);
    if (owned !== generation || readAbort !== controller) return null;
    readAbort = undefined;
    if (!result.ok) {
      patch({ status: "unavailable", refusal: result.code });
      return null;
    }
    adoptStatus(result.status);
    return result.status;
  }

  function adoptStatus(wire: MediaRuntimeStatus): void {
    patch({ status: "read", wire, refusal: null });
  }

  /** First Settings open: one read, then only what later actions return. */
  function mediaRuntimeEnsureStatus(): void {
    if (state().status !== "unread") return;
    void readStatus();
  }

  /** A contextual surface was refused by the runtime; learn why unless something already says. */
  function mediaRuntimeReport(feature: MediaRuntimeFeature): void {
    const current = state();
    if (current.status === "reading") return;
    // IMPORTANT: a surface remounts its card every time it re-reads its own capability, so a
    // report must not re-read a status that already explains the refusal: a feature the status
    // says is not ready, a status this feature's report already read, or a failed read (which
    // waits for Check again). Re-reading on every remount polled the status route once per
    // timeline edit in an open editor.
    if (current.status === "unavailable") return;
    if (
      current.wire !== null &&
      (current.wire.features[feature].state !== "ready" ||
        explained.get(feature) === current.wire)
    )
      return;
    void explain(feature);
  }

  /** Waits boundedly while a just-bound feature is still starting; `null` if the read failed. */
  async function settledStatus(
    feature: MediaRuntimeFeature,
    status: MediaRuntimeStatus,
    owned: number,
  ): Promise<MediaRuntimeStatus | null> {
    let current = status;
    for (
      let attempt = 0;
      attempt < MEDIA_RUNTIME_MAX_ACTIVATION_READS &&
      current.features[feature].state === "unavailable" &&
      (current.features[feature].reason === "activating" ||
        current.features[feature].reason === "discovering");
      attempt += 1
    ) {
      await new Promise((resolve) =>
        setTimeout(resolve, MEDIA_RUNTIME_POLL_INTERVAL_MS),
      );
      if (owned !== generation) return null;
      const next = await readStatus();
      if (owned !== generation || next === null) return null;
      current = next;
    }
    return current;
  }

  async function explain(feature: MediaRuntimeFeature): Promise<void> {
    const owned = generation;
    const read = await readStatus();
    if (owned !== generation || read === null) return;
    const status = await settledStatus(feature, read, owned);
    if (owned !== generation || status === null) return;
    explained.set(feature, state().wire);
    // The output capability is read once per overlay open, and the first open after a host start
    // can precede the in-process activation this status read triggers. A status that says final
    // output is ready re-reads the capability once; `explained` keeps a still-unsupported answer
    // from reading again.
    const surface = session.nleWorkspace.surface;
    if (
      feature === "render" &&
      status.features.render.state === "ready" &&
      surface.status === "expanded"
    )
      void Promise.resolve(
        actions.nleReadRenderCapability(surface.generation),
      ).catch(() => undefined);
  }

  async function statusAction(
    action: MediaRuntimeAction,
    payload: MediaRuntimeActionPayload = {},
  ): Promise<void> {
    if (state().busy !== null) return;
    // CRITICAL: a read begun before this mutation must not overwrite its newer config revision.
    readAbort?.abort();
    readAbort = undefined;
    const owned = generation;
    patch({ busy: action, refusal: null });
    const result: MediaRuntimeStatusResult = await deps.mediaRuntimeClient.send(
      action,
      payload,
    );
    if (owned !== generation) return;
    patch({ busy: null });
    if (result.ok && "status" in result) {
      adoptStatus(result.status);
      patch({ notice: "retry_action", focusPrimary: focusPrimary() });
      // IMPORTANT: refresh a capability only; choosing tools must never import, preview or render.
      const surface = session.nleWorkspace.surface;
      if (
        surface.status === "expanded" &&
        result.status.features.render.state === "ready"
      )
        void Promise.resolve(
          actions.nleReadRenderCapability(surface.generation),
        ).catch(() => undefined);
      return;
    }
    patch({
      refusal: result.ok ? "malformed_response" : result.code,
      focusPrimary: focusPrimary(),
    });
    if (!result.ok && result.outcomeUnknown) await readStatus();
  }

  function mediaRuntimeRescan(): void {
    // Rescan also recovers an unreadable status: the action answers with a fresh one.
    void statusAction("rescan");
  }

  function mediaRuntimeReclaim(): void {
    void statusAction("reclaim_parked_runtime");
  }

  function mediaRuntimeUseLocalDirectory(directory: string): void {
    const revision = state().wire?.config?.revision;
    if (revision === undefined) return;
    void statusAction("use_local_directory", {
      directory,
      expectedRevision: revision,
    });
  }

  function mediaRuntimeRestoreAuto(): void {
    const revision = state().wire?.config?.revision;
    if (revision === undefined) return;
    void statusAction("restore_auto", { expectedRevision: revision });
  }

  /** Release owns every outstanding response. */
  function releaseMediaRuntime(): void {
    generation += 1;
    readAbort?.abort();
    readAbort = undefined;
    explained.clear();
    session.mediaRuntime = initialMediaRuntimeState;
  }

  const bindingFunctions = Object.freeze({
    ensure: mediaRuntimeEnsureStatus,
    report: mediaRuntimeReport,
    dismiss: () => {
      if (state().notice !== null) patch({ notice: null });
    },
    rescan: mediaRuntimeRescan,
    reclaim: mediaRuntimeReclaim,
    useLocalDirectory: mediaRuntimeUseLocalDirectory,
    restoreAuto: mediaRuntimeRestoreAuto,
  });

  /** The binding every surface renders; only `state` changes between renders. */
  function mediaToolsBinding(): MediaToolsBinding {
    return Object.freeze({ ...bindingFunctions, state: state() });
  }

  return {
    mediaRuntimeEnsureStatus,
    mediaRuntimeReclaim,
    mediaRuntimeReport,
    mediaRuntimeRescan,
    mediaRuntimeRestoreAuto,
    mediaRuntimeUseLocalDirectory,
    mediaToolsBinding,
    releaseMediaRuntime,
  };
}

export type MediaRuntimeActions = ReturnType<typeof createMediaRuntimeSession>;
