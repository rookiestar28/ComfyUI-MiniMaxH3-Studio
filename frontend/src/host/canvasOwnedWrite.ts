// Owned canvas write transaction. This is the only module allowed to call the host's
// `loadGraphData`; every write and rollback passes the captured active-workflow identity
// as the fourth argument (M23-19 rule, guarded by scripts/architecture_fitness.py and
// tests/test_frontend_module_budget.py).

import {
  observeOwnedGraph,
  type OwnedGraphReference,
} from "./ownedGraphIdentity";
import type { ExistingContextAuthoringResult } from "./templateMaterialization";
import { type AppModeApp, AppModeError } from "./appModeContract";
import {
  readVisibleGraph,
  seamReady,
  seamUnavailable,
  type SeamResult,
} from "./hostSeams";

/**
 * Declare a canvas write this run performs as its own.
 *
 * `loadGraphData` makes the host *configure* the graph, and the host announces
 * that to every extension -- including this one, whose configure hook exists to
 * abandon a run when the **user** switches workflows underneath it. M17-20 D11
 * made materialization go through that same seam, so without a declaration a run
 * cancelled itself between writing the canvas and compiling it, and only a real
 * host could show it: a stubbed `loadGraphData` fires no host hook.
 *
 * The declaration is scoped to the one call, released in a `finally`, and says
 * nothing about which graph was written. A foreign configure arriving at any
 * other moment still cancels the run, which is the behaviour that keeps a user's
 * own workflow switch authoritative.
 */
export type OwnedGraphConfigure = () => () => void;

type WorkflowStore = NonNullable<
  NonNullable<AppModeApp["extensionManager"]>["workflow"]
>;

/**
 * The workflow store (frontend.app.extension_manager.workflow.*). Owned here because the
 * captured active-workflow identity is what every owned write passes as the fourth
 * `loadGraphData` argument.
 */
export function probeWorkflowStore(app: AppModeApp): SeamResult<WorkflowStore> {
  const store = app.extensionManager?.workflow;
  return store !== null && typeof store === "object"
    ? seamReady(store)
    : seamUnavailable("workflow_store_unavailable");
}

/** The graph writer (frontend.app.load_graph_data), probed without calling it. */
export function probeGraphWriter(
  app: AppModeApp,
): SeamResult<NonNullable<AppModeApp["loadGraphData"]>> {
  const write = app.loadGraphData;
  return typeof write === "function"
    ? seamReady(write)
    : seamUnavailable("load_graph_data_unavailable");
}

export type OwnedGraphWriteAuthority = Readonly<{
  beginConfigure: OwnedGraphConfigure | undefined;
  workflow: object;
}>;

type PendingGraphWriteAuthority = Readonly<{
  beginConfigure: OwnedGraphConfigure | undefined;
  workflow: object | null;
  workflowStore?: NonNullable<AppModeApp["extensionManager"]>["workflow"];
}>;

export function readActiveWorkflow(app: AppModeApp): object | undefined {
  const workflow = app.extensionManager?.workflow?.activeWorkflow;
  return workflow !== null &&
    typeof workflow === "object" &&
    !Array.isArray(workflow)
    ? workflow
    : undefined;
}

export function captureGraphWriteAuthority(
  app: AppModeApp,
  beginConfigure: OwnedGraphConfigure | undefined,
): PendingGraphWriteAuthority {
  const workflow = readActiveWorkflow(app);
  if (workflow !== undefined)
    return Object.freeze({ beginConfigure, workflow });

  const workflowStore = app.extensionManager?.workflow;
  const openWorkflows = workflowStore?.openWorkflows;
  if (
    workflowStore?.activeWorkflow !== null ||
    !Array.isArray(openWorkflows) ||
    openWorkflows.length !== 0 ||
    typeof app.loadGraphData !== "function"
  )
    throw new AppModeError(
      "incompatible_seam",
      "the active host workflow identity is unavailable",
    );

  // CRITICAL: reserve the legal tabless 0-to-1 transition without exercising
  // it. M23-25 validates the complete detached candidate first; that candidate,
  // not a disposable copy of the old canvas, is the one permitted null write.
  return Object.freeze({ beginConfigure, workflow: null, workflowStore });
}

export function assertGraphWriteAuthority(
  app: AppModeApp,
  authority: OwnedGraphWriteAuthority,
): void {
  if (readActiveWorkflow(app) !== authority.workflow)
    throw new AppModeError(
      "stale_graph",
      "the active host workflow changed during the App Mode run",
    );
}

export function assertPendingGraphWriteAuthority(
  app: AppModeApp,
  authority: PendingGraphWriteAuthority,
): void {
  if (authority.workflow !== null) {
    if (readActiveWorkflow(app) === authority.workflow) return;
  } else {
    const workflowStore = authority.workflowStore;
    if (
      workflowStore !== undefined &&
      app.extensionManager?.workflow === workflowStore &&
      workflowStore.activeWorkflow === null &&
      Array.isArray(workflowStore.openWorkflows) &&
      workflowStore.openWorkflows.length === 0
    )
      return;
  }
  throw new AppModeError(
    "stale_graph",
    "the host workflow changed before the validated graph write",
  );
}

async function loadOwnedGraph(
  app: AppModeApp,
  workflow: unknown,
  authority: OwnedGraphWriteAuthority,
): Promise<void> {
  // CRITICAL: omitting ComfyUI's fourth argument creates a new temporary
  // workflow. Keep every owned write and rollback on the captured active tab.
  assertGraphWriteAuthority(app, authority);
  const release = authority.beginConfigure?.();
  try {
    await Promise.resolve(
      app.loadGraphData!(workflow, true, true, authority.workflow),
    );
    assertGraphWriteAuthority(app, authority);
  } finally {
    release?.();
  }
}

export async function writeValidatedGraph(
  app: AppModeApp,
  candidate: unknown,
  pending: PendingGraphWriteAuthority,
): Promise<OwnedGraphWriteAuthority> {
  const existingAuthority = ownedAuthorityBeforeWrite(pending);
  if (existingAuthority !== undefined) {
    const authority = existingAuthority;
    await loadOwnedGraph(app, candidate, authority);
    return authority;
  }

  const workflowStore = pending.workflowStore;
  if (workflowStore === undefined || typeof app.loadGraphData !== "function")
    throw new AppModeError(
      "incompatible_seam",
      "the host workflow bootstrap seam is unavailable",
    );
  const release = pending.beginConfigure?.();
  try {
    await Promise.resolve(app.loadGraphData(candidate, true, true, null));
  } catch {
    throw new AppModeError(
      "compile_failed",
      "the host rejected the validated graph transaction",
    );
  } finally {
    release?.();
  }
  const workflow = readActiveWorkflow(app);
  const attached = workflowStore.openWorkflows;
  if (
    app.extensionManager?.workflow !== workflowStore ||
    workflow === undefined ||
    !Array.isArray(attached) ||
    attached.length !== 1 ||
    attached[0] !== workflow
  )
    throw new AppModeError(
      "incompatible_seam",
      "the validated graph write returned an incompatible workflow identity",
    );
  return Object.freeze({ beginConfigure: pending.beginConfigure, workflow });
}

export function ownedAuthorityBeforeWrite(
  pending: PendingGraphWriteAuthority,
): OwnedGraphWriteAuthority | undefined {
  return pending.workflow === null
    ? undefined
    : Object.freeze({
        beginConfigure: pending.beginConfigure,
        workflow: pending.workflow,
      });
}

export function ownedAuthorityAfterPartialBootstrap(
  app: AppModeApp,
  pending: PendingGraphWriteAuthority,
): OwnedGraphWriteAuthority | undefined {
  const existing = ownedAuthorityBeforeWrite(pending);
  if (existing !== undefined) return existing;
  const workflowStore = pending.workflowStore;
  const workflow = readActiveWorkflow(app);
  const attached = workflowStore?.openWorkflows;
  return workflowStore !== undefined &&
    app.extensionManager?.workflow === workflowStore &&
    workflow !== undefined &&
    Array.isArray(attached) &&
    attached.length === 1 &&
    attached[0] === workflow
    ? Object.freeze({ beginConfigure: pending.beginConfigure, workflow })
    : undefined;
}

export async function restoreGraph(
  app: AppModeApp,
  beforeLoad: unknown,
  authority: OwnedGraphWriteAuthority,
  ownedReference?: OwnedGraphReference,
): Promise<void> {
  if (beforeLoad === undefined || app.loadGraphData === undefined)
    throw new AppModeError(
      "rollback_failed",
      "the original canvas restore seam is unavailable",
    );
  try {
    const beforeOwned =
      ownedReference === undefined
        ? undefined
        : observeOwnedGraph(beforeLoad, ownedReference).fingerprint;
    await loadOwnedGraph(app, beforeLoad, authority);
    if (
      beforeOwned !== undefined &&
      observeOwnedGraph(readVisibleGraph(app), ownedReference!).fingerprint !==
        beforeOwned
    )
      throw new Error("owned graph restore identity did not match");
  } catch {
    throw new AppModeError(
      "rollback_failed",
      "the original canvas could not be restored safely",
    );
  }
}

export function existingOwnedReference(
  rebound: ExistingContextAuthoringResult,
): OwnedGraphReference {
  return Object.freeze({
    nodeIds: Object.freeze([
      rebound.requestNodeId,
      rebound.durationSourceNodeId,
    ]),
    linkIds: Object.freeze([]),
    anchorNodeId: rebound.anchorNodeId,
    authoredWidgetNodeIds: Object.freeze([
      rebound.requestNodeId,
      rebound.durationSourceNodeId,
    ]),
  });
}
