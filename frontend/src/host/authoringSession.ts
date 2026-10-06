// Authoring session: authoring wire payloads and intents, availability facts, assisted and
// generic workspace actions (M23-28 split of entry.tsx).

import type {
  AuthoringIntent,
  AuthoringViewState,
  UnknownTimelineOutcome,
} from "../state/authoringViewState";
import {
  initialSidebarStagesDraft,
  rebaseSidebarStagesDraft,
  sidebarStagesAuthority,
  type SidebarStagesDraft,
  type WorkspaceActionRequest,
} from "../components/SidebarStages";
import type { AssistedActionRequest } from "../contracts/assistedPromptProposalCodec";
import {
  authoringMoveGroupPayload,
  encodeTimelineTransactionV2,
  encodeTimelineTransaction,
  NLE_AUTHORING_PROFILE_ID,
  NLE_AUTHORING_SCHEMA,
  NLE_OPERATION_PROFILE_ID_V2,
  type AuthoringProjection,
  type TimelineHistoryProjection,
  type TimelineHistoryProjectionV2,
  type TimelineReceipt,
  type TimelineReceiptV2,
} from "../contracts/authoringWorkbenchCodec";
import {
  AuthoringClientError,
  type AuthoringClientResult,
} from "./authoringActions";
import { deriveAvailabilityFacts } from "./authoringAvailability";
import { isQualifiedExternalReferenceGraph } from "./graphAdapter";
import { reduceWorkspaceState } from "../state/sidebarWorkspace";
import { probeGraphToPrompt } from "./hostSeams";
import { type ShellRuntime } from "../lifecycle/shellSession";

// CRITICAL: a timeline transaction has a *known* outcome only when the backend's own protocol
// said so -- a decoded 409 rejection body, or a receipt decoded and bound to this request,
// transaction and CAS. Every failure listed here happened after the request left the browser
// with no such evidence, so the mutation may have been applied and the surface must say
// "unknown" rather than "rejected" (post-corrective review 02, R2-F1; the first corrective
// covered only `transport_failure` and let a body-read failure fall through to
// `internal_failure` against pre-submit history). Adding a code here must mean "the backend
// never told us": adding one that does establish an outcome would hide a real refusal, and
// removing one reinstates the false rejection. `cross_workspace_response` is deliberately
// absent -- a reply bound to another workspace is an integrity refusal, not outcome evidence.
const UNKNOWN_MUTATION_OUTCOME_CODES: ReadonlySet<string> = new Set([
  "transport_failure",
  "response_body_unavailable",
  "outcome_evidence_undecodable",
  "timeline_response_mismatch",
]);

export function createAuthoringSession(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;

  function retainedTimelineFields(
    timelineHistory: TimelineHistoryProjection | undefined,
    lastTimelineReceipt: TimelineReceipt | undefined,
    timelineHistoryV2?: TimelineHistoryProjectionV2,
    lastTimelineReceiptV2?: TimelineReceiptV2,
  ): Readonly<{
    timelineHistory?: TimelineHistoryProjection;
    lastTimelineReceipt?: TimelineReceipt;
    timelineHistoryV2?: TimelineHistoryProjectionV2;
    lastTimelineReceiptV2?: TimelineReceiptV2;
  }> {
    return {
      ...(timelineHistory === undefined ? {} : { timelineHistory }),
      ...(lastTimelineReceipt === undefined ? {} : { lastTimelineReceipt }),
      ...(timelineHistoryV2 === undefined ? {} : { timelineHistoryV2 }),
      ...(lastTimelineReceiptV2 === undefined ? {} : { lastTimelineReceiptV2 }),
    };
  }

  // M25-16 corrective F1. CRITICAL: a lost transport reply is neither a rejection nor an
  // acceptance -- the backend may have committed the transaction. Never re-send the draft (a
  // replay could apply it twice) and never claim a receipt for it; keep its identity for
  // attribution only, then reconcile with exactly one read-only history read. If that read
  // fails the state stays `outcome_unknown`; only the unknown state this read was issued for may
  // be replaced, because a release, later transaction or terminal reply already owns the surface.
  async function reconcileUnknownOutcome(
    projection: AuthoringProjection,
    handle: string,
    identity: Omit<UnknownTimelineOutcome, "reconciliation">,
    retainedHistory?: TimelineHistoryProjection,
    retainedHistoryV2?: TimelineHistoryProjectionV2,
  ): Promise<void> {
    const unknownWith = (
      reconciliation: UnknownTimelineOutcome["reconciliation"],
    ): AuthoringViewState => ({
      status: "error",
      projection,
      reason: "outcome_unknown",
      ...retainedTimelineFields(undefined, undefined, retainedHistoryV2),
      ...(retainedHistory === undefined
        ? {}
        : { timelineHistory: retainedHistory }),
      outcomeUnknown: { ...identity, reconciliation },
    });
    const unknown = unknownWith("pending");
    session.authoringState = unknown;
    actions.renderCurrent();
    try {
      const refreshed = await deps.authoringActions.send(
        nextAuthoringRequestId(),
        "read_timeline_history",
        { workspace_handle: handle },
        handle,
      );
      if (refreshed.history === undefined && refreshed.historyV2 === undefined)
        throw new Error("refreshed timeline history is absent");
      // CRITICAL: the late result is fenced by the exact state object this read owns. A close,
      // release or newer state during the read must survive it; adopting the read unconditionally
      // would reinstate a workspace the user already left.
      if (session.authoringState !== unknown) return;
      session.authoringState = {
        status: "ready",
        projection,
        ...(refreshed.historyV2 === undefined
          ? { timelineHistory: refreshed.history }
          : { timelineHistoryV2: refreshed.historyV2 }),
      };
    } catch (error) {
      if (session.authoringState !== unknown) return;
      session.authoringState =
        error instanceof AuthoringClientError &&
        (error.status === 404 || error.status === 410)
          ? { status: "gone", reason: error.code }
          : // The read could not establish the outcome either. Keep the uncertainty and say so:
            // the surface must not describe a failed read as a completed re-read.
            unknownWith("failed");
    }
    actions.renderCurrent();
  }

  const browserRequestSessionToken = (() => {
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);
    return [...bytes]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  })();

  function nextAuthoringRequestId(): string {
    session.authoringRequestCounter += 1;
    return `authoring-${browserRequestSessionToken}-${session.authoringRequestCounter}`;
  }

  async function sha256Fingerprint(text: string): Promise<string> {
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(text),
    );
    const hex = [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    return `sha256:${hex}`;
  }

  // M20-03 F1: the one qualified availability producer. The verdict is the boolean
  // graph-shape proof over the host's own compiled workflow; without a compilable
  // workflow there is no verdict and nothing is posted -- absence stays unknown.
  async function currentGraphQualificationVerdict(): Promise<Readonly<{
    qualified: boolean;
    graphFingerprint: string;
  }> | null> {
    const compile = probeGraphToPrompt(deps.app);
    if (compile.status !== "ready") return null;
    try {
      const compiled = (await Promise.resolve(compile.value())) as Readonly<{
        workflow?: unknown;
      }>;
      if (compiled.workflow === undefined) return null;
      return {
        qualified: isQualifiedExternalReferenceGraph(compiled.workflow),
        graphFingerprint: await sha256Fingerprint(
          JSON.stringify(compiled.workflow),
        ),
      };
    } catch {
      return null;
    }
  }

  function adoptAuthoringResult(result: AuthoringClientResult): void {
    if (result.projection !== undefined) {
      const timelineHistory =
        "timelineHistory" in session.authoringState
          ? session.authoringState.timelineHistory
          : undefined;
      const lastTimelineReceipt =
        "lastTimelineReceipt" in session.authoringState
          ? session.authoringState.lastTimelineReceipt
          : undefined;
      const timelineHistoryV2 =
        "timelineHistoryV2" in session.authoringState
          ? session.authoringState.timelineHistoryV2
          : undefined;
      const lastTimelineReceiptV2 =
        "lastTimelineReceiptV2" in session.authoringState
          ? session.authoringState.lastTimelineReceiptV2
          : undefined;
      session.authoringState = {
        status: result.status === 409 ? "conflict" : "ready",
        projection: result.projection,
        ...retainedTimelineFields(
          timelineHistory,
          lastTimelineReceipt,
          timelineHistoryV2,
          lastTimelineReceiptV2,
        ),
      };
    }
    actions.renderCurrent();
  }

  function authoringWirePayload(
    intent: AuthoringIntent,
    handle: string,
    referenceRevision: number,
    timelineRevision: number,
  ): Readonly<{
    action:
      | "add_source"
      | "remove_source"
      | "reorder_source"
      | "include_soundtrack"
      | "exclude_soundtrack"
      | "add_clip"
      | "remove_clip"
      | "move_clip"
      | "move_group"
      | "trim_clip"
      | "split_clip"
      | "merge_clips"
      | "link_clips"
      | "unlink_clips"
      | "select_clips";
    payload: Readonly<Record<string, unknown>>;
  }> | null {
    const reference = {
      workspace_handle: handle,
      expected_reference_revision: referenceRevision,
    };
    const timeline = {
      workspace_handle: handle,
      expected_timeline_revision: timelineRevision,
    };
    switch (intent.action) {
      case "add_source":
      case "remove_source":
        return {
          action: intent.action,
          payload: { ...reference, source_id: intent.sourceId },
        };
      case "reorder_source":
        return {
          action: intent.action,
          payload: {
            ...reference,
            source_id: intent.sourceId,
            new_index: intent.newIndex,
          },
        };
      case "include_soundtrack":
        return {
          action: intent.action,
          payload: {
            ...reference,
            video_id: intent.videoId,
            audio_id: intent.audioId,
          },
        };
      case "exclude_soundtrack":
        return {
          action: intent.action,
          payload: { ...reference, video_id: intent.videoId },
        };
      case "add_clip":
        return {
          action: intent.action,
          payload: {
            ...timeline,
            asset_id: intent.assetId,
            lane: intent.lane,
            start_frame: intent.startFrame,
            frames: intent.frames,
            source_start_frame: intent.sourceStartFrame,
          },
        };
      case "remove_clip":
        return {
          action: intent.action,
          payload: { ...timeline, clip_id: intent.clipId },
        };
      case "move_clip":
        return {
          action: intent.action,
          payload: {
            ...timeline,
            clip_id: intent.clipId,
            delta_frames: intent.deltaFrames,
            delta_lanes: intent.deltaLanes,
          },
        };
      case "move_group":
        return {
          action: intent.action,
          payload: authoringMoveGroupPayload({
            workspaceHandle: handle,
            expectedTimelineRevision: timelineRevision,
            clipIds: intent.clipIds,
            deltaFrames: intent.deltaFrames,
          }),
        };
      case "trim_clip":
        return {
          action: intent.action,
          payload: {
            ...timeline,
            clip_id: intent.clipId,
            edge: intent.edge,
            delta_frames: intent.deltaFrames,
          },
        };
      case "split_clip":
        return {
          action: intent.action,
          payload: {
            ...timeline,
            clip_id: intent.clipId,
            at_offset_frames: intent.atOffsetFrames,
          },
        };
      case "merge_clips":
        return {
          action: intent.action,
          payload: {
            ...timeline,
            first_clip_id: intent.firstClipId,
            second_clip_id: intent.secondClipId,
          },
        };
      case "link_clips":
        return {
          action: intent.action,
          payload: {
            ...timeline,
            video_clip_id: intent.videoClipId,
            audio_clip_id: intent.audioClipId,
          },
        };
      case "unlink_clips":
        return {
          action: intent.action,
          payload: { ...timeline, video_clip_id: intent.videoClipId },
        };
      case "select_clips":
        return {
          action: intent.action,
          payload: { ...timeline, clip_ids: [...intent.clipIds] },
        };
      default:
        return null;
    }
  }

  async function runAuthoringIntent(
    intent: AuthoringIntent,
    creationContextWorkspaceHandle?: string,
  ): Promise<void> {
    // One in-flight command at a time: a second dispatch would be computed from the
    // same stale expected revisions and could only ever earn a spurious conflict.
    if (
      session.authoringState.status === "pending" ||
      session.authoringState.status === "loading"
    )
      return;
    const previous =
      "projection" in session.authoringState
        ? session.authoringState.projection
        : undefined;
    const acceptedHistory: TimelineHistoryProjection | undefined =
      "timelineHistory" in session.authoringState
        ? session.authoringState.timelineHistory
        : undefined;
    const acceptedReceipt: TimelineReceipt | undefined =
      "lastTimelineReceipt" in session.authoringState
        ? session.authoringState.lastTimelineReceipt
        : undefined;
    const acceptedHistoryV2: TimelineHistoryProjectionV2 | undefined =
      "timelineHistoryV2" in session.authoringState
        ? session.authoringState.timelineHistoryV2
        : undefined;
    const acceptedReceiptV2: TimelineReceiptV2 | undefined =
      "lastTimelineReceiptV2" in session.authoringState
        ? session.authoringState.lastTimelineReceiptV2
        : undefined;
    const handle = previous?.workspaceHandle;
    try {
      if (intent.action === "create_authoring_workspace") {
        const contextHandle =
          creationContextWorkspaceHandle ?? currentProjection()?.workspace_id;
        if (contextHandle === undefined) return;
        session.authoringState = { status: "loading" };
        actions.renderCurrent();
        adoptAuthoringResult(
          await deps.authoringActions.send(
            nextAuthoringRequestId(),
            "create_authoring_workspace",
            { context_workspace_handle: contextHandle },
          ),
        );
        return;
      }
      if (previous === undefined || handle === undefined) return;
      session.authoringState = {
        status: "pending",
        projection: previous,
        ...retainedTimelineFields(
          acceptedHistory,
          acceptedReceipt,
          acceptedHistoryV2,
          acceptedReceiptV2,
        ),
      };
      actions.renderCurrent();
      if (
        intent.action === "read_timeline_history" ||
        intent.action === "initialize_timeline_history"
      ) {
        const read = () =>
          deps.authoringActions.send(
            nextAuthoringRequestId(),
            "read_timeline_history",
            { workspace_handle: handle },
            handle,
          );
        let result: AuthoringClientResult;
        if (intent.action === "initialize_timeline_history") {
          try {
            result = await deps.authoringActions.send(
              nextAuthoringRequestId(),
              "initialize_timeline_history",
              {
                workspace_handle: handle,
                expected_reference_revision: previous.reference.revision,
                expected_timeline_revision: previous.timeline.revision,
                authoring_schema: NLE_AUTHORING_SCHEMA,
                profile_id: NLE_AUTHORING_PROFILE_ID,
                operation_profile_id: NLE_OPERATION_PROFILE_ID_V2,
              },
              handle,
            );
          } catch (error) {
            // CRITICAL: an existing or concurrently initialized history wins. Read
            // its current branch heads after 409; never replace it with a new seed.
            if (
              !(error instanceof AuthoringClientError) ||
              error.code !== "timeline_initialization_conflict"
            )
              throw error;
            result = await read();
          }
        } else result = await read();
        if (result.history === undefined && result.historyV2 === undefined)
          throw new Error("timeline history response is absent");
        if (
          !("projection" in session.authoringState) ||
          session.authoringState.projection?.workspaceHandle !== handle
        )
          return;
        session.authoringState = {
          status: "ready",
          projection: previous,
          ...(result.historyV2 === undefined
            ? { timelineHistory: result.history }
            : { timelineHistoryV2: result.historyV2 }),
          ...retainedTimelineFields(
            undefined,
            acceptedReceipt,
            undefined,
            acceptedReceiptV2,
          ),
        };
        actions.renderCurrent();
        return;
      }
      if (intent.action === "apply_timeline_commands") {
        if (
          intent.capturedTimeline !== undefined &&
          intent.capturedTimeline.workspaceHandle !== handle
        )
          throw new Error("captured timeline workspace changed");
        const historyHandle =
          acceptedHistoryV2?.workspaceHandle ??
          acceptedHistory?.workspaceHandle;
        if (historyHandle === undefined || historyHandle !== handle)
          throw new Error("accepted timeline history is absent");
        const requestId = nextAuthoringRequestId();
        // IMPORTANT: pointer drafts use their captured CAS, never the latest history
        // at release; replacing it silently rebases a stale cut onto concurrent edits.
        const acceptedAuthoringV2 = acceptedHistoryV2?.authoring;
        const expected =
          intent.capturedTimeline ??
          acceptedAuthoringV2 ??
          acceptedHistory?.snapshot;
        if (expected === undefined || expected === null)
          throw new Error("accepted timeline render snapshot is absent");
        const transaction = acceptedAuthoringV2
          ? encodeTimelineTransactionV2({
              requestId,
              transactionId: `tx-${requestId}`,
              workspaceHandle: historyHandle,
              expectedWorkspaceRevision: expected.workspaceRevision,
              expectedTimelineRevision: expected.timelineRevision,
              expectedTimelineFingerprint: expected.timelineFingerprint,
              expectedAuthoringFingerprint:
                intent.capturedTimeline?.authoringFingerprint ??
                acceptedAuthoringV2.authoringFingerprint,
              commands: intent.commands,
            })
          : encodeTimelineTransaction({
              requestId,
              transactionId: `tx-${requestId}`,
              workspaceHandle: historyHandle,
              expectedWorkspaceRevision: expected.workspaceRevision,
              expectedTimelineRevision: expected.timelineRevision,
              expectedTimelineFingerprint: expected.timelineFingerprint,
              commands: intent.commands,
            });
        let result: AuthoringClientResult;
        try {
          result = await deps.authoringActions.send(
            requestId,
            "apply_timeline_transaction",
            transaction,
            handle,
          );
        } catch (error) {
          if (
            !(error instanceof AuthoringClientError) ||
            !UNKNOWN_MUTATION_OUTCOME_CODES.has(error.code)
          )
            throw error;
          await reconcileUnknownOutcome(
            previous,
            handle,
            {
              requestId,
              transactionId: `tx-${requestId}`,
              expectedWorkspaceRevision: expected.workspaceRevision,
              expectedTimelineRevision: expected.timelineRevision,
              expectedTimelineFingerprint: expected.timelineFingerprint,
              ...(acceptedAuthoringV2 === undefined
                ? {}
                : {
                    expectedAuthoringFingerprint:
                      intent.capturedTimeline?.authoringFingerprint ??
                      acceptedAuthoringV2.authoringFingerprint,
                  }),
            },
            acceptedHistory,
            acceptedHistoryV2,
          );
          return;
        }
        if (result.status === 409) {
          if (result.history === undefined && result.historyV2 === undefined)
            throw new Error("timeline conflict projection is absent");
          // CRITICAL: the conflict body is the sole accepted truth. Never retry a local
          // draft or retain its receipt after the backend rejected its exact CAS.
          session.authoringState = {
            status: "conflict",
            projection: previous,
            ...(result.historyV2 === undefined
              ? { timelineHistory: result.history }
              : { timelineHistoryV2: result.historyV2 }),
          };
          actions.renderCurrent();
          return;
        }
        if (result.receipt === undefined && result.receiptV2 === undefined)
          throw new Error("timeline receipt is absent");
        // CRITICAL: receipt acceptance advances backend state before the history read returns.
        // Drop the pre-transaction snapshot immediately so even the pending render cannot label
        // stale branch heads or clip state as the accepted post-transaction timeline.
        session.authoringState =
          result.receiptV2 === undefined
            ? {
                status: "pending",
                projection: previous,
                lastTimelineReceipt: result.receipt,
              }
            : {
                status: "pending",
                projection: previous,
                lastTimelineReceiptV2: result.receiptV2,
              };
        actions.renderCurrent();
        let refreshed: AuthoringClientResult;
        try {
          refreshed = await deps.authoringActions.send(
            nextAuthoringRequestId(),
            "read_timeline_history",
            { workspace_handle: handle },
            handle,
          );
          if (
            refreshed.history === undefined &&
            refreshed.historyV2 === undefined
          )
            throw new Error("refreshed timeline history is absent");
        } catch (error) {
          const code =
            error instanceof AuthoringClientError
              ? error.code
              : "internal_failure";
          const gone =
            error instanceof AuthoringClientError &&
            (error.status === 404 || error.status === 410);
          // CRITICAL: a terminal workspace invalidates every receipt and cursor even when this
          // transaction was accepted first. Never leave a terminal surface with actionable state.
          if (gone) {
            session.authoringState = { status: "gone", reason: code };
            actions.renderCurrent();
            return;
          }
          // The transaction is accepted, but its branch heads are unknown. Retain only that receipt
          // and force an explicit history reload; the pre-transaction history stays discarded.
          session.authoringState = {
            status: "error",
            projection: previous,
            reason: code,
            ...(result.receiptV2 === undefined
              ? { lastTimelineReceipt: result.receipt }
              : { lastTimelineReceiptV2: result.receiptV2 }),
          };
          actions.renderCurrent();
          return;
        }
        session.authoringState = {
          status: "ready",
          projection: previous,
          ...(refreshed.historyV2 === undefined
            ? { timelineHistory: refreshed.history }
            : { timelineHistoryV2: refreshed.historyV2 }),
          ...(result.receiptV2 === undefined
            ? { lastTimelineReceipt: result.receipt }
            : { lastTimelineReceiptV2: result.receiptV2 }),
        };
        actions.renderCurrent();
        return;
      }
      if (intent.action === "read_projection") {
        adoptAuthoringResult(
          await deps.authoringActions.send(
            nextAuthoringRequestId(),
            "read_projection",
            { workspace_handle: handle },
            handle,
          ),
        );
        return;
      }
      if (intent.action === "release_workspace") {
        await deps.authoringActions.send(
          nextAuthoringRequestId(),
          "release_workspace",
          { workspace_handle: handle },
        );
        session.authoringState = { status: "released" };
        actions.renderCurrent();
        return;
      }
      if (intent.action === "refresh_availability") {
        const verdict = await currentGraphQualificationVerdict();
        const payload = deriveAvailabilityFacts(previous, verdict);
        if (payload === null) {
          session.authoringState = {
            status: "ready",
            projection: previous,
            ...retainedTimelineFields(acceptedHistory, acceptedReceipt),
          };
          actions.renderCurrent();
          return;
        }
        adoptAuthoringResult(
          await deps.authoringActions.send(
            nextAuthoringRequestId(),
            "set_availability",
            { workspace_handle: handle, ...payload },
            handle,
          ),
        );
        return;
      }
      if (intent.action === "snap_clip") {
        const clip = previous.timeline.clips.find(
          (item) => item.clipId === intent.clipId,
        );
        if (clip === undefined) {
          session.authoringState = {
            status: "ready",
            projection: previous,
            ...retainedTimelineFields(acceptedHistory, acceptedReceipt),
          };
          actions.renderCurrent();
          return;
        }
        const snapped = await deps.authoringActions.send(
          nextAuthoringRequestId(),
          "read_snap",
          {
            workspace_handle: handle,
            frame: clip.startFrame,
            playhead_frame: null,
            exclude_clip_id: clip.clipId,
          },
          handle,
        );
        const candidate = snapped.snap?.candidates[0];
        if (candidate === undefined || candidate.frame === clip.startFrame) {
          session.authoringState = {
            status: "ready",
            projection: previous,
            ...retainedTimelineFields(acceptedHistory, acceptedReceipt),
          };
          actions.renderCurrent();
          return;
        }
        adoptAuthoringResult(
          await deps.authoringActions.send(
            nextAuthoringRequestId(),
            "move_clip",
            {
              workspace_handle: handle,
              expected_timeline_revision: previous.timeline.revision,
              clip_id: clip.clipId,
              delta_frames: candidate.frame - clip.startFrame,
              delta_lanes: 0,
            },
            handle,
          ),
        );
        return;
      }
      const wire = authoringWirePayload(
        intent,
        handle,
        previous.reference.revision,
        previous.timeline.revision,
      );
      if (wire === null) {
        session.authoringState = {
          status: "ready",
          projection: previous,
          ...retainedTimelineFields(acceptedHistory, acceptedReceipt),
        };
        actions.renderCurrent();
        return;
      }
      adoptAuthoringResult(
        await deps.authoringActions.send(
          nextAuthoringRequestId(),
          wire.action,
          wire.payload,
          handle,
        ),
      );
    } catch (error) {
      const code =
        error instanceof AuthoringClientError ? error.code : "internal_failure";
      const gone =
        error instanceof AuthoringClientError &&
        (error.status === 404 || error.status === 410);
      // CRITICAL: 404/410 is terminal for this opaque handle. Keeping any projection, receipt or
      // branch cursor would let compact controls operate on state the backend has already released.
      if (gone) {
        session.authoringState = { status: "gone", reason: code };
        actions.renderCurrent();
        return;
      }
      const retainedHistory =
        "timelineHistory" in session.authoringState
          ? session.authoringState.timelineHistory
          : acceptedHistory;
      const retainedReceipt =
        "lastTimelineReceipt" in session.authoringState
          ? session.authoringState.lastTimelineReceipt
          : acceptedReceipt;
      const retainedHistoryV2 =
        "timelineHistoryV2" in session.authoringState
          ? session.authoringState.timelineHistoryV2
          : acceptedHistoryV2;
      const retainedReceiptV2 =
        "lastTimelineReceiptV2" in session.authoringState
          ? session.authoringState.lastTimelineReceiptV2
          : acceptedReceiptV2;
      session.authoringState = {
        status: "error",
        projection: previous,
        reason: code,
        ...retainedTimelineFields(
          retainedHistory,
          retainedReceipt,
          retainedHistoryV2,
          retainedReceiptV2,
        ),
      };
      actions.renderCurrent();
    }
  }

  function currentWorkspaceDraft(): SidebarStagesDraft | undefined {
    const projection = currentProjection();
    if (projection === undefined) return undefined;
    if (
      session.workspaceDraft === undefined ||
      session.workspaceDraft.authority !== sidebarStagesAuthority(projection)
    )
      session.workspaceDraft =
        session.workspaceDraft === undefined
          ? initialSidebarStagesDraft(projection)
          : rebaseSidebarStagesDraft(projection, session.workspaceDraft);
    return session.workspaceDraft;
  }

  function updateWorkspaceDraft(next: SidebarStagesDraft): void {
    const projection = currentProjection();
    if (
      projection === undefined ||
      next.authority !== sidebarStagesAuthority(projection)
    )
      return;
    session.workspaceDraft = next;
    actions.renderCurrent();
  }

  function currentProjection() {
    return session.workspaceState.status === "awaiting"
      ? undefined
      : session.workspaceState.projection;
  }

  function downloadTransfer(value: object): void {
    const blob = new Blob([JSON.stringify(value, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "h3-context-prompt.json";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  function isAssistedAction(
    request: WorkspaceActionRequest,
  ): request is AssistedActionRequest {
    return (
      request.action === "optimize_prompt" ||
      request.action === "refine_prompt" ||
      request.action === "edit_assisted_proposal" ||
      request.action === "accept_assisted_proposal" ||
      request.action === "reject_assisted_proposal" ||
      request.action === "cancel_assisted_execution"
    );
  }

  let cancellationOwner: object | undefined;
  async function runAssistedWorkspaceAction(
    projection: NonNullable<ReturnType<typeof currentProjection>>,
    request: AssistedActionRequest,
  ): Promise<void> {
    if (request.action === "cancel_assisted_execution") {
      if (cancellationOwner !== undefined) return;
      const owner = {};
      cancellationOwner = owner;
      const providerHandle = session.providerSessionHandle;
      // IMPORTANT: cancel must invalidate the client completion before its server ACK.
      // Keep the send lane busy until that ACK so it cannot cancel a newer action's lease.
      session.assistedAbort?.abort();
      session.assistedAbort = undefined;
      session.assistedProposal = undefined;
      session.assistedBusy = true;
      actions.renderCurrent();
      const ownsSource = () => {
        const current = currentProjection();
        return (
          current?.workspace_id === projection.workspace_id &&
          current.report_revision === projection.report_revision &&
          current.report_fingerprint === projection.report_fingerprint &&
          session.providerSessionHandle === providerHandle
        );
      };
      try {
        await deps.actions.sendAssisted(projection, request);
      } catch {
        if (ownsSource())
          session.assistedFailure = "assisted.client_request_failed";
      } finally {
        if (cancellationOwner === owner) {
          cancellationOwner = undefined;
          if (ownsSource() && session.assistedAbort === undefined)
            session.assistedBusy = false;
        }
      }
      actions.renderCurrent();
      return;
    }
    if (session.assistedBusy) return;
    const abort = new AbortController();
    session.assistedAbort?.abort();
    session.assistedAbort = abort;
    session.assistedBusy = true;
    session.assistedFailure = undefined;
    const actionDraft = currentWorkspaceDraft();
    actions.renderCurrent();
    try {
      const result = await deps.actions.sendAssisted(
        projection,
        request,
        abort.signal,
      );
      if (abort.signal.aborted || session.assistedAbort !== abort) return;
      const current = currentProjection();
      if (
        current?.workspace_id !== projection.workspace_id ||
        current.report_revision !== projection.report_revision ||
        current.report_fingerprint !== projection.report_fingerprint
      )
        return;
      if (result.kind === "workspace") {
        const newerDraft = currentWorkspaceDraft();
        session.workspaceState = reduceWorkspaceState(session.workspaceState, {
          type: "received",
          projection: result.projection,
        });
        session.assistedProposal = undefined;
        session.workspaceDraft = rebaseSidebarStagesDraft(
          result.projection,
          currentWorkspaceDraft() ??
            initialSidebarStagesDraft(result.projection),
        );
        // IMPORTANT: accepting a proposal changes report authority, but an edit made while
        // that request was pending still belongs to the user. Rebase its authority, not its text.
        if (
          actionDraft !== undefined &&
          newerDraft !== undefined &&
          newerDraft.promptText !== actionDraft.promptText
        ) {
          session.workspaceDraft = {
            ...session.workspaceDraft,
            promptText: newerDraft.promptText,
            reason: newerDraft.reason,
          };
        }
        return;
      }
      if (result.proposal?.state === "active") {
        session.assistedProposal = result.proposal;
        session.assistedFailure = undefined;
        return;
      }
      if (
        result.proposal?.state === "rejected" ||
        result.proposal?.state === "cancelled"
      ) {
        session.assistedProposal = undefined;
        session.assistedFailure = undefined;
        return;
      }
      if (result.result.state === "failed")
        session.assistedFailure =
          result.result.outcomeId ?? "assisted.client_request_failed";
      if (result.result.state === "cancelled")
        session.assistedProposal = undefined;
    } catch {
      if (!abort.signal.aborted && session.assistedAbort === abort)
        session.assistedFailure = "assisted.client_request_failed";
    } finally {
      if (session.assistedAbort === abort) {
        session.assistedAbort = undefined;
        session.assistedBusy = false;
      }
      actions.renderCurrent();
    }
  }

  async function runWorkspaceAction(
    request: WorkspaceActionRequest,
  ): Promise<void> {
    const projection = currentProjection();
    if (projection === undefined) return;
    if (isAssistedAction(request)) {
      await runAssistedWorkspaceAction(projection, request);
      return;
    }
    if (session.workspaceState.status === "loading") return;
    session.actionAbort?.abort();
    const abort = new AbortController();
    session.actionAbort = abort;
    session.workspaceState = reduceWorkspaceState(session.workspaceState, {
      type: "request",
      action: request.action,
    });
    actions.renderCurrent();
    try {
      const result = await deps.actions.send(projection, request, abort.signal);
      if (abort.signal.aborted || session.actionAbort !== abort) return;
      if (result.kind === "workspace") {
        session.assistedProposal = undefined;
        session.assistedFailure = undefined;
        session.workspaceState = reduceWorkspaceState(session.workspaceState, {
          type: "received",
          projection: result.projection,
        });
      } else {
        downloadTransfer(result.transfer);
        session.workspaceState = reduceWorkspaceState(session.workspaceState, {
          type: "received",
          projection,
        });
      }
    } catch {
      if (!abort.signal.aborted && session.actionAbort === abort)
        session.workspaceState = reduceWorkspaceState(session.workspaceState, {
          type: "failed",
        });
    } finally {
      if (session.actionAbort === abort) session.actionAbort = undefined;
      actions.renderCurrent();
    }
  }

  function reportWorkspaceFailure(): void {
    session.workspaceState = reduceWorkspaceState(session.workspaceState, {
      type: "failed",
    });
    actions.renderCurrent();
  }

  return {
    adoptAuthoringResult,
    authoringWirePayload,
    browserRequestSessionToken,
    currentGraphQualificationVerdict,
    currentProjection,
    currentWorkspaceDraft,
    downloadTransfer,
    isAssistedAction,
    nextAuthoringRequestId,
    reportWorkspaceFailure,
    runAssistedWorkspaceAction,
    runAuthoringIntent,
    runWorkspaceAction,
    sha256Fingerprint,
    updateWorkspaceDraft,
  };
}
