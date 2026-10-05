// M25-33 Media tools session: status reads, the one setup job, and the validated continuation of
// the user action a setup interrupted.
//
// Reads happen only when someone can use them: the first Settings open, a contextual surface that
// reports a runtime refusal, and a 1 s job poll that stops at the terminal state or on release.
// A continuation runs the original action once, and only after every identity it captured still
// holds. Nothing here queues, submits a workflow, uploads or starts a render.

import type {
  MediaRuntimeAction,
  MediaRuntimeFeature,
  MediaRuntimeJob,
  MediaRuntimeStatus,
} from "../contracts/mediaRuntimeCodec";
import type {
  MediaRuntimeActionPayload,
  MediaRuntimeJobResult,
  MediaRuntimeStatusResult,
} from "../host/mediaRuntimeClient";
import {
  MEDIA_RUNTIME_INTENT_FEATURE,
  initialMediaRuntimeState,
  intentIdentity,
  type MediaRuntimeIntent,
  type MediaRuntimeNotice,
  type MediaRuntimeState,
  type MediaToolsBinding,
} from "../state/mediaRuntimeState";
import type { ShellRuntime } from "./shellSession";

export const MEDIA_RUNTIME_POLL_INTERVAL_MS = 1_000;
/** Consecutive unreadable job polls before the surface reports status unavailable. */
export const MEDIA_RUNTIME_MAX_POLL_FAILURES = 30;
/** Status re-reads while a just-installed feature is still activating. */
export const MEDIA_RUNTIME_MAX_ACTIVATION_READS = 10;

export type MediaRuntimeLeaveScope = "page" | "function" | "overlay";

export function createMediaRuntimeSession(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;
  // IMPORTANT: every asynchronous result checks the generation it started under. Release bumps
  // it, so a late status, job or action response cannot repopulate a released view or continue
  // an action the user has already walked away from.
  let generation = 0;
  let readAbort: AbortController | undefined;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let pollingJob: string | undefined;
  let resumeToken = 0;
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
    const running = wire.setup?.state === "running" ? wire.setup : null;
    patch({
      status: "read",
      wire,
      job: wire.setup ?? state().job,
      refusal: null,
    });
    // A job started by another tab or before a lost response is shared, never started again.
    if (running !== null) startPolling(running.jobId);
  }

  /** First Settings open: one read, then only what later actions return. */
  function mediaRuntimeEnsureStatus(): void {
    if (state().status !== "unread") return;
    void readStatus();
  }

  /** A contextual surface was refused by the runtime; learn why unless something already says. */
  function mediaRuntimeReport(feature: MediaRuntimeFeature): void {
    const current = state();
    if (current.status === "reading" || pollingJob !== undefined) return;
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

  // ------------------------------------------------------------------ job polling

  function stopPolling(): void {
    if (pollTimer !== undefined) clearTimeout(pollTimer);
    pollTimer = undefined;
    pollingJob = undefined;
  }

  function startPolling(jobId: string): void {
    if (pollingJob === jobId) return;
    stopPolling();
    pollingJob = jobId;
    const owned = generation;
    let failures = 0;
    const tick = async (): Promise<void> => {
      pollTimer = undefined;
      if (owned !== generation || pollingJob !== jobId) return;
      const result: MediaRuntimeJobResult =
        await deps.mediaRuntimeClient.readJob(jobId);
      if (owned !== generation || pollingJob !== jobId) return;
      if (result.ok) {
        failures = 0;
        patch({ job: result.job });
        if (result.job.state === "running") {
          pollTimer = setTimeout(tick, MEDIA_RUNTIME_POLL_INTERVAL_MS);
          return;
        }
        pollingJob = undefined;
        await settleJob(result.job);
        return;
      }
      if (result.code === "setup_job_not_found") {
        pollingJob = undefined;
        // The server no longer knows the job (a host restart); keeping it would leave the card
        // installing forever, because a status without `setup` never replaces the session's job.
        patch({ job: null });
        await readStatus();
        return;
      }
      failures += 1;
      if (failures >= MEDIA_RUNTIME_MAX_POLL_FAILURES) {
        pollingJob = undefined;
        patch({ status: "unavailable", refusal: result.code });
        return;
      }
      pollTimer = setTimeout(tick, MEDIA_RUNTIME_POLL_INTERVAL_MS);
    };
    pollTimer = setTimeout(tick, MEDIA_RUNTIME_POLL_INTERVAL_MS);
  }

  async function settleJob(job: MediaRuntimeJob): Promise<void> {
    const owned = generation;
    const status = await readStatus();
    if (owned !== generation || status === null) return;
    const pending = state().pending;
    if (job.state === "cancelled") {
      patch({
        pending: null,
        notice: pending === null ? state().notice : "continuation_cleared",
        focusPrimary: focusPrimary(),
      });
      return;
    }
    if (job.state !== "succeeded") {
      // The continuation stays: Install again resumes the same action if it still holds.
      patch({ focusPrimary: focusPrimary() });
      return;
    }
    if (pending === null) return;
    // The binding is published before consumers finish attaching, so a feature can report
    // `activating` for a moment after a successful install. Wait for it, boundedly.
    const settled = await settledStatus(
      MEDIA_RUNTIME_INTENT_FEATURE[pending.kind],
      status,
      owned,
    );
    if (owned !== generation || settled === null) return;
    continueIntent(settled);
  }

  // ------------------------------------------------------------------ continuation

  function productionProjection() {
    return "projection" in session.productionState
      ? session.productionState.projection
      : undefined;
  }

  function overlayStillOpen(generationValue: number): boolean {
    const surface = session.nleWorkspace.surface;
    return (
      surface.status === "expanded" && surface.generation === generationValue
    );
  }

  /** Whether every identity the intent captured still holds right now. */
  function revalidate(intent: MediaRuntimeIntent): boolean {
    switch (intent.kind) {
      case "production_import": {
        const production = productionProjection();
        const importStatus = session.nleWorkspace.import.status;
        return (
          production !== undefined &&
          deps.pageRegistry.getSnapshot().selected === "production" &&
          production.workspaceHandle === intent.productionWorkspaceHandle &&
          production.workspaceId === intent.productionWorkspaceId &&
          production.allowedActions.includes(
            "import_production_outputs_to_authoring",
          ) &&
          importStatus !== "importing" &&
          importStatus !== "ensuring_target" &&
          importStatus !== "uncertain" &&
          // IMPORTANT (M25-36): a generated segment can advance the project revision while
          // setup is running. The selected ready outputs are the import authority; pinning the
          // revision here discards that still-valid action after every automatic append.
          production.selectedSegmentIds.length === intent.segmentIds.length &&
          production.selectedSegmentIds.every(
            (segmentId, index) => segmentId === intent.segmentIds[index],
          ) &&
          intent.segmentIds.every(
            (segmentId, index) =>
              production.outputs.find(
                (row) => row.segmentId === segmentId && row.state === "ready",
              )?.outputHandle === intent.outputHandles[index],
          )
        );
      }
      case "clip_preview": {
        const authoring = session.authoringState;
        const projection =
          "projection" in authoring ? authoring.projection : undefined;
        return (
          projection !== undefined &&
          projection.workspaceHandle === intent.workspaceHandle &&
          projection.reference.revision === intent.referenceRevision &&
          projection.timeline.revision === intent.timelineRevision &&
          projection.timeline.contentFingerprint ===
            intent.timelineContentFingerprint &&
          projection.timeline.clips.some(
            (clip) => clip.clipId === intent.clipId,
          ) &&
          (intent.overlayGeneration === null ||
            overlayStillOpen(intent.overlayGeneration))
        );
      }
      case "final_render": {
        const authoring = session.authoringState;
        const historyV2 =
          "timelineHistoryV2" in authoring
            ? authoring.timelineHistoryV2
            : undefined;
        const receiptV2 =
          "lastTimelineReceiptV2" in authoring
            ? authoring.lastTimelineReceiptV2
            : undefined;
        const snapshot =
          historyV2 !== undefined
            ? (historyV2.renderSnapshot ?? undefined)
            : receiptV2 !== undefined
              ? (receiptV2.renderSnapshot ?? undefined)
              : (("timelineHistory" in authoring
                  ? authoring.timelineHistory?.snapshot
                  : undefined) ??
                ("lastTimelineReceipt" in authoring
                  ? authoring.lastTimelineReceipt?.snapshot
                  : undefined));
        return (
          snapshot !== undefined &&
          snapshot.workspaceHandle === intent.workspaceHandle &&
          snapshot.workspaceRevision === intent.workspaceRevision &&
          snapshot.timelineRevision === intent.timelineRevision &&
          snapshot.publicFingerprint === intent.publicFingerprint &&
          overlayStillOpen(intent.overlayGeneration)
        );
      }
    }
  }

  function continueIntent(status: MediaRuntimeStatus): void {
    const intent = state().pending;
    if (intent === null) return;
    const feature = MEDIA_RUNTIME_INTENT_FEATURE[intent.kind];
    if (status.features[feature].state !== "ready") {
      patch({
        pending: null,
        notice: "feature_not_ready",
        focusPrimary: focusPrimary(),
      });
      return;
    }
    if (!revalidate(intent)) {
      patch({
        pending: null,
        notice: "select_again",
        focusPrimary: focusPrimary(),
      });
      return;
    }
    // CRITICAL: clear the continuation before running it. Running first would let a render that
    // the action itself triggers observe the same pending intent and run it a second time.
    resumeToken += 1;
    patch({
      pending: null,
      notice: "continued",
      resume: Object.freeze({
        kind: intent.kind,
        token: resumeToken,
        identity: intentIdentity(intent),
      }),
    });
    if (intent.kind === "production_import")
      void Promise.resolve(
        actions.nleImportSelectedOutputs(intent.segmentIds),
      ).catch(() => undefined);
    else if (intent.kind === "final_render")
      void Promise.resolve(
        actions.nleReadRenderCapability(intent.overlayGeneration),
      ).catch(() => undefined);
    // `clip_preview` is reopened by the one monitor whose identity matches `resume.identity`.
  }

  function recordPending(intent: MediaRuntimeIntent | null): void {
    if (intent === null) return;
    const current = state().pending;
    if (current !== null && intentIdentity(current) === intentIdentity(intent))
      return;
    patch({
      pending: intent,
      notice: current === null ? null : "continuation_replaced",
    });
  }

  function clearPending(notice: MediaRuntimeNotice = "continuation_cleared") {
    if (state().pending === null) return;
    patch({ pending: null, notice, focusPrimary: focusPrimary() });
  }

  /** Leaving a continuation's context drops it; nothing continues somewhere the user left. */
  function mediaRuntimeLeaveContext(scope: MediaRuntimeLeaveScope): void {
    const pending = state().pending;
    if (pending === null) return;
    const overlayBound =
      pending.kind === "final_render" ||
      (pending.kind === "clip_preview" && pending.overlayGeneration !== null);
    if (
      scope === "page" ||
      (scope === "overlay" && overlayBound) ||
      (scope === "function" && !overlayBound)
    )
      clearPending();
  }

  // ------------------------------------------------------------------ actions

  async function mediaRuntimeInstall(
    intent: MediaRuntimeIntent | null,
  ): Promise<void> {
    const current = state();
    // IMPORTANT: a second click while the first request is in flight is not a second install.
    if (current.busy !== null) return;
    recordPending(intent);
    if (current.job?.state === "running") {
      startPolling(current.job.jobId);
      return;
    }
    const owned = generation;
    patch({ busy: "install_supported", notice: null, refusal: null });
    const result = await deps.mediaRuntimeClient.send("install_supported", {});
    if (owned !== generation) return;
    patch({ busy: null });
    if (!result.ok) {
      if (result.outcomeUnknown) {
        const status = await readStatus();
        if (owned !== generation) return;
        if (status?.setup?.state !== "running")
          patch({ notice: "outcome_unknown", focusPrimary: focusPrimary() });
        return;
      }
      patch({ refusal: result.code, focusPrimary: focusPrimary() });
      if (result.code === "setup_busy") await readStatus();
      return;
    }
    if (!("job" in result)) return;
    patch({ job: result.job });
    if (result.job.state === "running") startPolling(result.job.jobId);
    else await settleJob(result.job);
  }

  async function mediaRuntimeCancel(): Promise<void> {
    const job = state().job;
    if (job === null || job.state !== "running" || state().busy !== null)
      return;
    const owned = generation;
    patch({
      busy: "cancel_setup",
      pending: null,
      notice:
        state().pending === null ? state().notice : "continuation_cleared",
    });
    const result = await deps.mediaRuntimeClient.send("cancel_setup", {
      jobId: job.jobId,
    });
    if (owned !== generation) return;
    patch({
      busy: null,
      ...(result.ok && "job" in result
        ? { job: result.job }
        : { refusal: result.ok ? null : result.code }),
      focusPrimary: focusPrimary(),
    });
  }

  async function statusAction(
    action: Exclude<MediaRuntimeAction, "install_supported" | "cancel_setup">,
    payload: MediaRuntimeActionPayload = {},
  ): Promise<void> {
    if (state().busy !== null) return;
    const owned = generation;
    patch({ busy: action, refusal: null });
    const result: MediaRuntimeStatusResult | MediaRuntimeJobResult =
      await deps.mediaRuntimeClient.send(action, payload);
    if (owned !== generation) return;
    patch({ busy: null });
    if (result.ok && "status" in result) {
      adoptStatus(result.status);
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

  /** View release: stop polling, drop requests and continuations, keep nothing. */
  function releaseMediaRuntime(): void {
    generation += 1;
    readAbort?.abort();
    readAbort = undefined;
    stopPolling();
    explained.clear();
    session.mediaRuntime = initialMediaRuntimeState;
  }

  const bindingFunctions = Object.freeze({
    ensure: mediaRuntimeEnsureStatus,
    report: mediaRuntimeReport,
    install: (intent: MediaRuntimeIntent | null) =>
      void mediaRuntimeInstall(intent),
    cancel: () => void mediaRuntimeCancel(),
    // Dismissing a notice is acknowledgement only; the continuation, if any, is kept.
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
    mediaRuntimeCancel,
    mediaRuntimeEnsureStatus,
    mediaRuntimeInstall,
    mediaRuntimeLeaveContext,
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
