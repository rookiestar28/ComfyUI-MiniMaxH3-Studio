import {
  createProjectFileSession,
  type ProjectFileBinding,
} from "./projectFileSession";
import type {
  ProjectOwner,
  ProjectSnapshotOwner,
  ProjectOpened,
  ProjectRelinked,
  ProjectPlanning,
} from "../contracts/projectDocumentCodec";
import {
  initialNlePlanningState,
  initialNleReadinessState,
} from "../state/nleWorkspaceState";
import type { ShellRuntime } from "./shellSession";
import { createProjectRecoverySession } from "./projectRecoverySession";
export function createProjectPersistenceSession(ctx: ShellRuntime) {
  const { session, deps, actions } = ctx;
  let openedOwner: ProjectOwner | null = null;
  let retained: NonNullable<ProjectFileBinding["retained"]> = [];
  let previous: ReturnType<typeof bookmark> | undefined;
  let recovery: ReturnType<typeof createProjectRecoverySession> | undefined;
  function bookmark() {
    return {
      production: session.productionState,
      authoring: session.authoringState,
      nle: session.nleWorkspace,
      draft: session.appModeDraft,
      owner: openedOwner,
      handle: session.productionSessionHandle,
      contextBinding: session.productionContextBinding,
      metadata: file?.binding().state,
    };
  }
  function owner(): ProjectSnapshotOwner | null {
    const production =
      "projection" in session.productionState
        ? session.productionState.projection
        : undefined;
    const view = session.authoringState;
    const authoring = "projection" in view ? view.projection : undefined;
    const history =
      "timelineHistoryV2" in view ? view.timelineHistoryV2 : undefined;
    const receipt =
      "lastTimelineReceiptV2" in view ? view.lastTimelineReceiptV2 : undefined;
    const state =
      receipt !== undefined &&
      (history === undefined ||
        receipt.authoring.timelineRevision >=
          history.authoring.timelineRevision)
        ? receipt.authoring
        : history?.authoring;
    if (authoring === undefined || state === undefined) {
      if (production !== undefined)
        return {
          production_handle: production.workspaceHandle,
          production_id: production.workspaceId,
          production_revision: production.workspaceRevision,
          production_fingerprint: production.workspaceFingerprint,
          authoring_handle: null,
          reference_revision: null,
          legacy_timeline_revision: null,
          workspace_revision: null,
          timeline_revision: null,
          authoring_fingerprint: null,
        };
      return null;
    }
    const paired =
      openedOwner?.authoring_handle === authoring.workspaceHandle
        ? openedOwner
        : null;
    return {
      production_handle:
        production?.workspaceHandle ?? paired?.production_handle ?? null,
      production_id: production?.workspaceId ?? paired?.production_id ?? null,
      production_revision:
        production?.workspaceRevision ?? paired?.production_revision ?? null,
      production_fingerprint:
        production?.workspaceFingerprint ??
        paired?.production_fingerprint ??
        null,
      authoring_handle: authoring.workspaceHandle,
      reference_revision: authoring.reference.revision,
      legacy_timeline_revision: authoring.timeline.revision,
      workspace_revision: state.workspaceRevision,
      timeline_revision: state.timelineRevision,
      authoring_fingerprint: state.authoringFingerprint,
    } as ProjectSnapshotOwner;
  }
  function planning(): ProjectPlanning {
    const value = session.nleWorkspace.planning;
    return {
      intent: session.appModeDraft.userIntent,
      script: value.script ?? "",
      target_seconds: value.targetSeconds,
      policy: value.policy,
      shots: value.storyboardRows.map((row) => ({
        shot_id: row.shotId,
        ordinal: row.ordinal,
        start_milliseconds: row.startMilliseconds,
        end_milliseconds: row.endMilliseconds,
        text: row.text,
        hard_boundary: row.hardBoundary,
      })),
    };
  }
  function adopt(value: ProjectOpened | ProjectRelinked) {
    if ("planning" in value) {
      previous = bookmark();
      session.productionAbort?.abort();
      session.productionAbort = undefined;
      session.productionState =
        value.production === null
          ? { status: "absent" }
          : { status: "ready", projection: value.production };
      session.productionSessionHandle = value.owner.production_handle;
      session.productionContextBinding = undefined;
      session.retryableProductionRequest = undefined;
      session.appModeDraft = {
        ...session.appModeDraft,
        userIntent: value.planning.intent,
      };
      session.appModeDraftRevision += 1;
      session.nleWorkspace = {
        ...session.nleWorkspace,
        planning: {
          ...initialNlePlanningState,
          script: value.planning.script,
          targetSeconds: value.planning.target_seconds,
          policy: value.planning.policy,
          storyboardRows: value.planning.shots.map((row) => ({
            shotId: row.shot_id,
            ordinal: row.ordinal,
            startMilliseconds: row.start_milliseconds,
            endMilliseconds: row.end_milliseconds,
            text: row.text,
            hardBoundary: row.hard_boundary,
          })),
        },
        readiness: initialNleReadinessState,
      };
      // SECURITY: portable data is not adopted into the workflow's generation destination.
      actions.closeProductionMediaPreview(false);
    }
    session.productionState =
      value.production === null
        ? { status: "absent" }
        : { status: "ready", projection: value.production };
    openedOwner = value.owner;
    session.authoringState = {
      status: "ready",
      projection: value.authoring,
      timelineHistoryV2: value.history,
    };
    actions.nleSyncOwnerRenewal?.();
  }
  const file =
    deps.projectDocumentClient === undefined
      ? undefined
      : createProjectFileSession({
          client: deps.projectDocumentClient,
          changed: () => actions.renderCurrent(),
          capture() {
            const pair = owner(),
              draft = planning();
            return {
              owner: pair,
              planning: draft,
              title: "",
              key: JSON.stringify([pair, draft]),
            };
          },
          adopt,
          discard: (value) => deps.projectDocumentClient!.discard(value.owner),
          beforeReplace: () =>
            recovery?.beforeReplace(false) ?? Promise.resolve(true),
        });
  if (deps.projectRecoveryClient !== undefined && file !== undefined) {
    recovery = createProjectRecoverySession({
      client: deps.projectRecoveryClient,
      changed: () => actions.renderCurrent(),
      capture: () => ({
        owner: owner(),
        planning: planning(),
        title: file.binding().state.title,
      }),
      adopt(value) {
        adopt(value);
        file.restoreMetadata({
          title: value.title,
          missingMedia: value.missingMedia,
        });
      },
      discard: (value) => deps.projectDocumentClient!.discard(value.owner),
      visible: () =>
        session.container !== undefined &&
        document.visibilityState === "visible",
    });
  }
  async function refreshRetained() {
    const result = await deps.retainedAssetsClient?.send({ intent: "list" });
    retained =
      result?.ok && result.response.projection.enabled
        ? result.response.projection.assets.filter(
            (row) => row.state !== "expired",
          )
        : [];
    actions.renderCurrent();
  }
  async function restorePrevious() {
    if (previous === undefined || file === undefined) return;
    if (recovery !== undefined && !(await recovery.beforeReplace(false)))
      return;
    const current = bookmark(),
      saved = previous;
    session.productionState = saved.production;
    session.authoringState = saved.authoring;
    session.nleWorkspace = saved.nle;
    session.appModeDraft = saved.draft;
    session.productionSessionHandle = saved.handle;
    openedOwner = saved.owner;
    previous = current;
    session.productionContextBinding = saved.contextBinding;
    if (saved.metadata !== undefined) file.restoreMetadata(saved.metadata);
    actions.nleSyncOwnerRenewal?.();
    actions.renderCurrent();
  }
  return {
    projectFileBinding(): ProjectFileBinding | undefined {
      if (file === undefined) return undefined;
      return {
        ...file.binding(),
        recovery: recovery?.binding(),
        retained,
        refreshRetained,
        blocked:
          ["pending", "loading"].includes(session.authoringState.status) ||
          ["pending", "loading"].includes(session.productionState.status) ||
          session.activeManagedRun !== undefined,
        ...(previous === undefined
          ? {}
          : {
              previous: () => {
                void restorePrevious();
              },
            }),
      };
    },
    projectRecoveryObserve: () => recovery?.observe(),
    projectRecoveryLeave: () => recovery?.leave(),
    projectRecoveryDispose: () => recovery?.dispose(),
    projectFileDispose: () => {
      file?.dispose();
      recovery?.leave();
    },
  };
}
