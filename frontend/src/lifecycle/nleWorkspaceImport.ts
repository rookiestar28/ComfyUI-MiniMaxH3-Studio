// M25-16 NLE workspace import slice: the explicit Production-output to Authoring import
// (M25-29) as issued from the workspace. Split from `nleWorkspaceSession.ts` to keep every
// lifecycle module within the M23-28 line budget; it owns nothing beyond the in-flight import
// transport and reads shared session state only through the core it is given.
//
// Every import is one existing accepted action with one retained idempotent request identity.
// Nothing here decides eligibility beyond the projection's own allowed actions, and nothing
// here mounts, plans, qualifies or queues.

import type { ProductionAuthoringImportRequest } from "../contracts/productionAuthoringImportCodec";
import { ProductionAuthoringImportError } from "../host/productionAuthoringImportClient";
import {
  initialNleImportState,
  type NleImportState,
  type NleWorkspaceState,
} from "../state/nleWorkspaceState";
import type { ShellRuntime } from "./shellSession";

type ProductionProjection = Extract<
  ShellRuntime["session"]["productionState"],
  { projection: unknown }
>["projection"];

export type NleImportSessionCore = Readonly<{
  ctx: ShellRuntime;
  state: () => NleWorkspaceState;
  patch: (next: Partial<NleWorkspaceState>) => void;
  nextRequestId: (prefix: string) => string;
  productionProjection: () => ProductionProjection | undefined;
  /** Monotonic view epoch; a result landing after a later epoch must not steer focus. */
  viewIntentEpoch: () => number;
  focusClipEditor: () => void;
}>;

export function createNleWorkspaceImport(core: NleImportSessionCore) {
  const { session, deps, actions } = core.ctx;
  const { state, patch, nextRequestId, productionProjection } = core;
  let importAbort: AbortController | undefined;
  let unrequestedOwnerKey: string | null = null;
  const retainedImports = new Map<string, NleImportState>();

  function ownerKey(handle: string, id: string): string {
    return `${handle}\u0000${id}`;
  }

  function requestIsCurrent(
    request: ProductionAuthoringImportRequest,
  ): boolean {
    const current = productionProjection();
    return (
      current?.workspaceHandle === request.productionWorkspaceHandle &&
      current.workspaceId === request.productionWorkspaceId
    );
  }

  function authoringCasChanged(
    original: ProductionAuthoringImportRequest,
    recaptured: ProductionAuthoringImportRequest,
  ): boolean {
    const commonChanged =
      recaptured.expectedProductionWorkspaceRevision !==
        original.expectedProductionWorkspaceRevision ||
      recaptured.expectedProductionWorkspaceFingerprint !==
        original.expectedProductionWorkspaceFingerprint ||
      recaptured.expectedAuthoringRegistryFingerprint !==
        original.expectedAuthoringRegistryFingerprint ||
      recaptured.expectedAuthoringReferenceRevision !==
        original.expectedAuthoringReferenceRevision ||
      recaptured.expectedAuthoringTimelineRevision !==
        original.expectedAuthoringTimelineRevision ||
      recaptured.expectedAuthoringTimelineContentFingerprint !==
        original.expectedAuthoringTimelineContentFingerprint ||
      recaptured.expectedNleWorkspaceRevision !==
        original.expectedNleWorkspaceRevision ||
      recaptured.expectedNleTimelineRevision !==
        original.expectedNleTimelineRevision ||
      recaptured.expectedNleTimelineFingerprint !==
        original.expectedNleTimelineFingerprint;
    if (commonChanged) return true;
    const originalIsV1 = "expectedNlePublicFingerprint" in original;
    const recapturedIsV1 = "expectedNlePublicFingerprint" in recaptured;
    if (originalIsV1 !== recapturedIsV1) return true;
    if (originalIsV1 && recapturedIsV1)
      return (
        original.expectedNlePublicFingerprint !==
        recaptured.expectedNlePublicFingerprint
      );
    if (!originalIsV1 && !recapturedIsV1)
      return (
        original.expectedNleAuthoringFingerprint !==
          recaptured.expectedNleAuthoringFingerprint ||
        original.authoringSchema !== recaptured.authoringSchema ||
        original.profileId !== recaptured.profileId
      );
    return true;
  }

  function nleImportStateForCurrentOwner(): NleImportState {
    const current = state().import;
    const owner = productionProjection();
    const activeKey =
      owner === undefined
        ? null
        : ownerKey(owner.workspaceHandle, owner.workspaceId);
    if (
      current.request === null
        ? current.status === "idle" || unrequestedOwnerKey === activeKey
        : requestIsCurrent(current.request)
    )
      return current;
    return owner === undefined
      ? initialNleImportState
      : (retainedImports.get(
          ownerKey(owner.workspaceHandle, owner.workspaceId),
        ) ?? initialNleImportState);
  }

  // IMPORTANT: retain the outgoing owner's outcome before any refusal or patch replaces the shared
  // import state. An uncertain outcome holds the only safe replay identity; losing it lets a later
  // import send a fresh request ID for work the backend may already have applied.
  function retainPreviousImport(): boolean {
    const current = state().import;
    if (current.request === null || requestIsCurrent(current.request))
      return true;
    if (retainedImports.size >= 64) {
      const removable = [...retainedImports].find(
        ([, outcome]) => outcome.status !== "uncertain",
      )?.[0];
      if (removable === undefined) return false;
      retainedImports.delete(removable);
    }
    retainedImports.set(
      ownerKey(
        current.request.productionWorkspaceHandle,
        current.request.productionWorkspaceId,
      ),
      current,
    );
    return true;
  }

  function importEligibility(segmentIds: readonly string[]) {
    const projection = productionProjection();
    if (projection === undefined)
      return { ok: false as const, reason: "target_unavailable" as const };
    if (
      !projection.allowedActions.includes(
        "import_production_outputs_to_authoring",
      )
    )
      return {
        ok: false as const,
        reason: "ineligible_or_unsupported" as const,
      };
    const ordered = projection.segments
      .map((segment) => segment.segmentId)
      .filter((segmentId) => segmentIds.includes(segmentId));
    if (
      ordered.length === 0 ||
      ordered.length !== segmentIds.length ||
      ordered.length > 3
    )
      return { ok: false as const, reason: "invalid_request" as const };
    const entries = ordered.map((segmentId) => {
      const output = projection.outputs.find(
        (row) => row.segmentId === segmentId && row.state === "ready",
      );
      return output === undefined
        ? null
        : { segmentId, outputHandle: output.outputHandle };
    });
    if (entries.some((entry) => entry === null))
      return {
        ok: false as const,
        reason: "ineligible_or_unsupported" as const,
      };
    return {
      ok: true as const,
      projection,
      entries: entries as { segmentId: string; outputHandle: string }[],
    };
  }

  /** Whether the compact Production action may be offered for this ordered selection. */
  function nleImportSelectionState(segmentIds: readonly string[]) {
    const eligibility = importEligibility(segmentIds);
    return {
      eligible:
        eligibility.ok &&
        nleImportStateForCurrentOwner().status !== "uncertain",
      reason: eligibility.ok ? null : eligibility.reason,
      busy:
        importAbort !== undefined ||
        nleImportStateForCurrentOwner().status === "importing" ||
        nleImportStateForCurrentOwner().status === "ensuring_target",
    };
  }

  /**
   * Whether a refreshed eligibility still names exactly the captured owner and outputs.
   * IMPORTANT: the import route admits only currently selected segments, so selection membership
   * is part of the identity; recapturing a deselected segment posts a request that must be refused.
   */
  function capturedEntriesStillCurrent(
    eligibility: ReturnType<typeof importEligibility>,
    request: ProductionAuthoringImportRequest,
  ): boolean {
    return (
      eligibility.ok &&
      eligibility.projection.workspaceHandle ===
        request.productionWorkspaceHandle &&
      eligibility.projection.workspaceId === request.productionWorkspaceId &&
      eligibility.entries.length === request.entries.length &&
      eligibility.entries.every(
        (entry, index) =>
          entry.segmentId === request.entries[index]?.segmentId &&
          entry.outputHandle === request.entries[index]?.outputHandle &&
          eligibility.projection.selectedSegmentIds.includes(entry.segmentId),
      )
    );
  }

  async function ensureAuthoringTarget(pickupEpoch: number): Promise<boolean> {
    const production = productionProjection();
    if (production === undefined) return false;
    const previous =
      "projection" in session.authoringState
        ? session.authoringState.projection
        : undefined;
    try {
      // CRITICAL: a retained browser Context handle can expire before its Production output.
      // Resolve the editor through the live exact Production owner; never claim the latest
      // visible child Context or silently substitute a different generated output.
      const result = await deps.authoringActions.send(
        nextRequestId("ensure-authoring"),
        "ensure_authoring_from_production",
        {
          production_workspace_handle: production.workspaceHandle,
          production_workspace_id: production.workspaceId,
          preferred_authoring_handle: previous?.workspaceHandle ?? null,
        },
      );
      if (result.projection === undefined) return false;
      if (
        pickupEpoch !== core.viewIntentEpoch() ||
        productionProjection()?.workspaceHandle !==
          production.workspaceHandle ||
        productionProjection()?.workspaceId !== production.workspaceId
      )
        return false;
      session.authoringState = {
        status: "ready",
        projection: result.projection,
        ...(previous?.workspaceHandle === result.projection.workspaceHandle &&
        "timelineHistory" in session.authoringState &&
        session.authoringState.timelineHistory !== undefined
          ? { timelineHistory: session.authoringState.timelineHistory }
          : {}),
        ...(previous?.workspaceHandle === result.projection.workspaceHandle &&
        "timelineHistoryV2" in session.authoringState &&
        session.authoringState.timelineHistoryV2 !== undefined
          ? { timelineHistoryV2: session.authoringState.timelineHistoryV2 }
          : {}),
      };
    } catch {
      return false;
    }
    if (
      !("projection" in session.authoringState) ||
      session.authoringState.projection === undefined
    )
      return false;
    const historyV1 =
      "timelineHistory" in session.authoringState
        ? session.authoringState.timelineHistory
        : undefined;
    const historyV2 =
      "timelineHistoryV2" in session.authoringState
        ? session.authoringState.timelineHistoryV2
        : undefined;
    if (historyV1 === undefined && historyV2 === undefined)
      await actions.runAuthoringIntent({
        action: "initialize_timeline_history",
      });
    else
      // Cached history is a display hint, not proof that the target remains current.
      await actions.runAuthoringIntent({ action: "read_timeline_history" });
    return (
      session.authoringState.status === "ready" &&
      (("timelineHistory" in session.authoringState &&
        session.authoringState.timelineHistory !== undefined) ||
        ("timelineHistoryV2" in session.authoringState &&
          session.authoringState.timelineHistoryV2 !== undefined))
    );
  }

  function captureImportRequest(
    entries: readonly { segmentId: string; outputHandle: string }[],
  ): ProductionAuthoringImportRequest | undefined {
    const production = productionProjection();
    const authoring =
      "projection" in session.authoringState
        ? session.authoringState.projection
        : undefined;
    const history =
      "timelineHistory" in session.authoringState
        ? session.authoringState.timelineHistory
        : undefined;
    const historyV2 =
      "timelineHistoryV2" in session.authoringState
        ? session.authoringState.timelineHistoryV2
        : undefined;
    if (
      production === undefined ||
      authoring === undefined ||
      (history === undefined && historyV2 === undefined)
    )
      return undefined;
    const entriesSnapshot = Object.freeze(
      entries.map((entry) => Object.freeze({ ...entry })),
    );
    const requestId = nextRequestId("import");
    const common = {
      requestId,
      productionWorkspaceHandle: production.workspaceHandle,
      productionWorkspaceId: production.workspaceId,
      expectedProductionWorkspaceRevision: production.workspaceRevision,
      expectedProductionWorkspaceFingerprint: production.workspaceFingerprint,
      authoringWorkspaceHandle: authoring.workspaceHandle,
      expectedAuthoringRegistryFingerprint: authoring.registryFingerprint,
      expectedAuthoringReferenceRevision: authoring.reference.revision,
      expectedAuthoringTimelineRevision: authoring.timeline.revision,
      expectedAuthoringTimelineContentFingerprint:
        authoring.timeline.contentFingerprint,
      entries: entriesSnapshot,
    };
    if (historyV2 !== undefined)
      return Object.freeze({
        ...common,
        expectedNleWorkspaceRevision: historyV2.authoring.workspaceRevision,
        expectedNleTimelineRevision: historyV2.authoring.timelineRevision,
        expectedNleTimelineFingerprint: historyV2.authoring.timelineFingerprint,
        expectedNleAuthoringFingerprint:
          historyV2.authoring.authoringFingerprint,
        authoringSchema: historyV2.authoring.schema,
        profileId: historyV2.authoring.profileId,
      });
    if (history === undefined) return undefined;
    return Object.freeze({
      ...common,
      expectedNleWorkspaceRevision: history.snapshot.workspaceRevision,
      expectedNleTimelineRevision: history.snapshot.timelineRevision,
      expectedNleTimelineFingerprint: history.snapshot.timelineFingerprint,
      expectedNlePublicFingerprint: history.snapshot.publicFingerprint,
    });
  }

  async function sendImport(
    request: ProductionAuthoringImportRequest,
  ): Promise<ProductionAuthoringImportError | null | undefined> {
    const abort = new AbortController();
    let failureResult: ProductionAuthoringImportError | null = null;
    importAbort = abort;
    patch({
      import: Object.freeze({
        ...state().import,
        status: "importing",
        requestId: request.requestId,
        request,
        refusal: null,
      }),
    });
    try {
      const response = await deps.productionAuthoringImportClient.send(
        request,
        abort.signal,
      );
      if (importAbort !== abort) return;
      // CRITICAL: a successful write belongs to its captured Production owner. Navigation
      // must not install that owner's editor projection into a different active project.
      const ownerStillCurrent = requestIsCurrent(request);
      // Adopt the returned Authoring projection, then refresh canonical history through the
      // existing bounded read so catalog/workspace identity advances even when the timeline
      // identity did not change. An import response is never a timeline transaction receipt.
      if (ownerStillCurrent) {
        const retainedHistory =
          "timelineHistory" in session.authoringState
            ? session.authoringState.timelineHistory
            : undefined;
        if (
          response.schema ===
          "h3.context.production_authoring_import.response.v2"
        )
          session.authoringState = {
            status: "ready",
            projection: response.authoringProjection,
            timelineHistoryV2: response.historyProjection,
          };
        else
          session.authoringState = {
            status: "ready",
            projection: response.authoringProjection,
            ...(retainedHistory === undefined
              ? {}
              : { timelineHistory: retainedHistory }),
          };
      }
      patch({
        import: Object.freeze({
          status: "succeeded",
          editorStatus: "unverified",
          requestId: request.requestId,
          request,
          receipt: response.receipt,
          refusal: null,
          highlightedAssetIds: Object.freeze(
            response.receipt.rows.map((row) => row.assetId),
          ),
        }),
      });
    } catch (error) {
      if (importAbort !== abort) return;
      const failure =
        error instanceof ProductionAuthoringImportError
          ? error
          : new ProductionAuthoringImportError("transport_failure", null, true);
      failureResult = failure;
      patch({
        import: Object.freeze({
          ...state().import,
          status: failure.outcomeUnknown ? "uncertain" : "refused",
          editorStatus: "unverified",
          requestId: request.requestId,
          request,
          receipt: null,
          refusal: failure.code,
          highlightedAssetIds: Object.freeze([]),
        }),
      });
      if (failure.code === "conflict_or_replay" || failure.status === 409) {
        try {
          await actions.runProductionIntent({ action: "read_projection" });
        } catch {
          // The known import refusal remains the owned outcome when reconciliation fails.
        }
      }
    } finally {
      if (importAbort === abort) importAbort = undefined;
    }
    // IMPORTANT: the import response above is the accepted outcome; these reads only refresh
    // derived state. They run outside the try so a failed or late refresh is reported through
    // the authoring/production state it belongs to and can never relabel a succeeded import
    // as uncertain, which would arm an explicit retry to resend an already-applied request.
    if (state().import.status !== "succeeded") return failureResult;
    if (requestIsCurrent(request)) {
      await openAcceptedImport(false);
      await Promise.resolve(
        actions.runProductionIntent({ action: "read_projection" }),
      ).catch(() => undefined);
    }
    return null;
  }

  async function openAcceptedImport(focus: boolean): Promise<void> {
    const accepted = state().import;
    const request = accepted.request;
    const receipt = accepted.receipt;
    if (accepted.status !== "succeeded" || request === null || receipt === null)
      return;
    if (!requestIsCurrent(request)) return;
    const pickupEpoch = core.viewIntentEpoch();
    let historyReadSucceeded = false;
    try {
      const currentAuthoring = session.authoringState;
      if (
        currentAuthoring.status !== "ready" ||
        currentAuthoring.projection.workspaceHandle !==
          request.authoringWorkspaceHandle
      ) {
        // A later project may have replaced the visible editor. Resolve the exact original
        // association before reading; a read against the later editor would corrupt the view.
        historyReadSucceeded = await ensureAuthoringTarget(pickupEpoch);
      } else {
        await actions.runAuthoringIntent({ action: "read_timeline_history" });
        historyReadSucceeded = true;
      }
    } catch {
      // IMPORTANT: the import already committed. A failed editor read must retain its receipt
      // and offer an opening retry; resending import can duplicate or overwrite user work.
    }
    const current = state().import;
    const authoring = session.authoringState;
    const historyV1 =
      "timelineHistory" in authoring ? authoring.timelineHistory : undefined;
    const historyV2 =
      "timelineHistoryV2" in authoring
        ? authoring.timelineHistoryV2
        : undefined;
    const importedAssets =
      historyV2?.authoring.assets ?? historyV1?.snapshot.assets;
    const ready =
      historyReadSucceeded &&
      current.status === "succeeded" &&
      current.requestId === request.requestId &&
      current.receipt === receipt &&
      productionProjection()?.workspaceHandle ===
        request.productionWorkspaceHandle &&
      productionProjection()?.workspaceId === request.productionWorkspaceId &&
      authoring.status === "ready" &&
      authoring.projection.workspaceHandle ===
        request.authoringWorkspaceHandle &&
      ((historyV1 !== undefined &&
        historyV1.workspaceHandle === request.authoringWorkspaceHandle) ||
        (historyV2 !== undefined &&
          historyV2.workspaceHandle === request.authoringWorkspaceHandle)) &&
      importedAssets !== undefined &&
      receipt.rows.every((row) =>
        importedAssets.some((asset) => asset.assetId === row.assetId),
      );
    if (current.requestId !== request.requestId) return;
    patch({
      import: Object.freeze({
        ...current,
        editorStatus: ready ? "ready" : "needs_action",
      }),
    });
    if (ready && focus && pickupEpoch === core.viewIntentEpoch())
      core.focusClipEditor();
  }

  async function nleOpenImportedEditor(): Promise<void> {
    const owned = nleImportStateForCurrentOwner();
    if (owned !== state().import) {
      if (!retainPreviousImport()) return;
      patch({ import: owned });
    }
    await openAcceptedImport(true);
  }

  /** Explicit `Import selected to editor`: ensure target, capture identities once, import. */
  async function nleImportSelectedOutputs(
    segmentIds: readonly string[],
  ): Promise<void> {
    const current = nleImportStateForCurrentOwner();
    // IMPORTANT: uncertainty retains one exact ledger identity. Only explicit replay
    // may resolve it; a new import must not replace the request with a fresh ID.
    if (
      importAbort !== undefined ||
      current.status === "importing" ||
      current.status === "ensuring_target" ||
      current.status === "uncertain"
    )
      return;
    if (!retainPreviousImport()) return;
    const owner = productionProjection();
    unrequestedOwnerKey =
      owner === undefined
        ? null
        : ownerKey(owner.workspaceHandle, owner.workspaceId);
    const pickupEpoch = core.viewIntentEpoch();
    const eligibility = importEligibility(segmentIds);
    if (!eligibility.ok) {
      patch({
        import: Object.freeze({
          ...initialNleImportState,
          status: "refused",
          refusal: eligibility.reason,
        }),
      });
      return;
    }
    patch({
      import: Object.freeze({
        ...initialNleImportState,
        status: "ensuring_target",
      }),
    });
    const ready = await ensureAuthoringTarget(pickupEpoch);
    if (
      pickupEpoch !== core.viewIntentEpoch() ||
      productionProjection()?.workspaceHandle !==
        eligibility.projection.workspaceHandle ||
      productionProjection()?.workspaceId !== eligibility.projection.workspaceId
    ) {
      session.nleWorkspace = Object.freeze({
        ...session.nleWorkspace,
        import: initialNleImportState,
      });
      return;
    }
    const request = ready
      ? captureImportRequest(eligibility.entries)
      : undefined;
    if (request === undefined) {
      patch({
        import: Object.freeze({
          ...initialNleImportState,
          status: "refused",
          refusal: "target_unavailable",
        }),
      });
      return;
    }
    const firstFailure = await sendImport(request);
    if (
      firstFailure !== null &&
      firstFailure !== undefined &&
      !firstFailure.outcomeUnknown &&
      (firstFailure.status === 404 ||
        firstFailure.status === 409 ||
        firstFailure.status === 410) &&
      pickupEpoch === core.viewIntentEpoch()
    ) {
      // IMPORTANT: recapture at most once after a known refusal. A lost reply may already
      // have committed, and changed output/editor identity could import into the wrong work.
      if (firstFailure.status !== 409) {
        try {
          await actions.runProductionIntent({ action: "read_projection" });
        } catch {
          return;
        }
      }
      if (
        capturedEntriesStillCurrent(importEligibility(segmentIds), request) &&
        pickupEpoch === core.viewIntentEpoch() &&
        (await ensureAuthoringTarget(pickupEpoch))
      ) {
        const currentSelection = importEligibility(segmentIds);
        const recaptured =
          currentSelection.ok &&
          capturedEntriesStillCurrent(currentSelection, request)
            ? captureImportRequest(currentSelection.entries)
            : undefined;
        if (
          recaptured?.authoringWorkspaceHandle ===
            request.authoringWorkspaceHandle &&
          authoringCasChanged(request, recaptured) &&
          pickupEpoch === core.viewIntentEpoch()
        )
          await sendImport(recaptured);
      }
    }
    // A background result may update canonical state, but cannot undo later navigation.
    const settled = state().import;
    if (
      settled.status === "succeeded" &&
      settled.editorStatus === "ready" &&
      settled.request !== null &&
      requestIsCurrent(settled.request) &&
      pickupEpoch === core.viewIntentEpoch()
    )
      core.focusClipEditor();
  }

  /** Explicit retry of the exact retained request after a lost response (same ID, same body). */
  async function nleRetryImport(): Promise<void> {
    const current = nleImportStateForCurrentOwner();
    if (current.status !== "uncertain" || current.request === null) return;
    if (importAbort !== undefined || !requestIsCurrent(current.request)) return;
    if (current !== state().import) {
      if (!retainPreviousImport()) return;
      patch({ import: current });
    }
    await sendImport(current.request);
  }

  function nleClearImportHighlight(): void {
    const current = nleImportStateForCurrentOwner();
    if (current.highlightedAssetIds.length === 0 && current.status === "idle")
      return;
    if (current !== state().import) {
      if (!retainPreviousImport()) return;
      patch({ import: current });
    }
    patch({
      import: Object.freeze({
        ...current,
        highlightedAssetIds: Object.freeze([]),
      }),
    });
  }

  /**
   * An import in flight at view destroy has an unknown outcome: abort the transport, drop
   * whatever lands later, and keep the exact request so an explicit retry replays the same
   * idempotent identity. Adopting a late success here would focus a destroyed view. Writes the
   * session directly because the caller resets the surface in the same tick without a render.
   */
  function abortAtViewDestroy(): void {
    if (importAbort === undefined) return;
    importAbort.abort();
    importAbort = undefined;
    const current = state().import;
    if (current.status === "importing")
      session.nleWorkspace = Object.freeze({
        ...session.nleWorkspace,
        import: Object.freeze({
          ...current,
          status: "uncertain",
          receipt: null,
          refusal: "transport_failure",
          highlightedAssetIds: Object.freeze([]),
        }),
      });
  }

  return {
    importEligibility,
    nleImportSelectionState,
    nleImportSelectedOutputs,
    nleRetryImport,
    nleImportStateForCurrentOwner,
    nleOpenImportedEditor,
    nleClearImportHighlight,
    abortAtViewDestroy,
  };
}
