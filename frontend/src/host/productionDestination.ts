import type {
  ProductionDestinationTarget,
  ProductionWorkbenchProjection,
} from "../contracts/productionWorkbenchCodec";

/**
 * M25-36: where the next App Mode Start of one workflow adds its clip.
 *
 * A destination is scoped to the host's live workflow object, never to a title, prompt or the most
 * recent project in the process. The only persisted fact is the last bound project's opaque handle,
 * id and default-name ordinal, so a reload can show the same name when that project is adopted.
 */
export const PRODUCTION_DESTINATION_STORAGE_KEY =
  "h3.context.production.destination.v1";

export type ProductionDestination =
  | Readonly<{
      kind: "project";
      workspaceHandle: string;
      workspaceId: string;
      ordinal: number;
    }>
  | Readonly<{ kind: "new"; ordinal: number }>
  /** The bound project expired or was released; only New project leaves this state. */
  | Readonly<{ kind: "unavailable"; workspaceHandle: string; ordinal: number }>;

type DestinationStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

type StoredDestination = Readonly<{
  workspaceHandle: string;
  workspaceId: string;
  ordinal: number;
  nextOrdinal: number;
}>;

const handlePattern = /^pw_[A-Za-z0-9_-]{32,96}$/;
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const MAX_ORDINAL = 1_000_000;
const MAX_PROJECT_OWNERS = 16;

function readStored(
  storage: DestinationStorage | undefined,
): StoredDestination | undefined {
  try {
    const raw = storage?.getItem(PRODUCTION_DESTINATION_STORAGE_KEY);
    if (typeof raw !== "string") return undefined;
    const value = JSON.parse(raw) as Record<string, unknown>;
    if (
      value === null ||
      typeof value !== "object" ||
      Object.keys(value).sort().join() !==
        "next_ordinal,ordinal,schema,workspace_handle,workspace_id" ||
      value.schema !== PRODUCTION_DESTINATION_STORAGE_KEY ||
      typeof value.workspace_handle !== "string" ||
      !handlePattern.test(value.workspace_handle) ||
      typeof value.workspace_id !== "string" ||
      !idPattern.test(value.workspace_id) ||
      !Number.isInteger(value.ordinal) ||
      !Number.isInteger(value.next_ordinal) ||
      (value.ordinal as number) < 1 ||
      (value.next_ordinal as number) <= (value.ordinal as number) ||
      (value.next_ordinal as number) > MAX_ORDINAL
    )
      return undefined;
    return Object.freeze({
      workspaceHandle: value.workspace_handle,
      workspaceId: value.workspace_id,
      ordinal: value.ordinal as number,
      nextOrdinal: value.next_ordinal as number,
    });
  } catch {
    return undefined;
  }
}

export function destinationTarget(
  destination: ProductionDestination,
  segmentId: string | null = null,
): ProductionDestinationTarget {
  if (destination.kind === "new") return null;
  if (destination.kind === "unavailable")
    throw new Error("an unavailable production destination admits nothing");
  return Object.freeze({
    workspaceHandle: destination.workspaceHandle,
    workspaceId: destination.workspaceId,
    segmentId,
  });
}

export function createProductionDestinationStore({
  storage,
}: {
  storage?: DestinationStorage;
} = {}) {
  const byWorkflow = new WeakMap<object, ProductionDestination>();
  // A host without a workflow store has exactly one canvas; it uses this slot.
  let unscoped: ProductionDestination | undefined;
  // CRITICAL: a project belongs to the workflow that first bound it. Adopting it from another
  // workflow merges two unrelated canvases' clips into one project, which nothing can undo.
  const owners = new Map<string, object | null>();
  const unavailable = new Set<string>();
  let stored = readStored(storage);
  let nextOrdinal = stored?.nextOrdinal ?? 1;

  const key = (workflow: object | undefined): object | null => workflow ?? null;

  function read(
    workflow: object | undefined,
  ): ProductionDestination | undefined {
    const scope = key(workflow);
    const value = scope === null ? unscoped : byWorkflow.get(scope);
    if (value?.kind === "project" && unavailable.has(value.workspaceHandle))
      return Object.freeze({
        kind: "unavailable",
        workspaceHandle: value.workspaceHandle,
        ordinal: value.ordinal,
      });
    return value;
  }

  function write(
    workflow: object | undefined,
    value: ProductionDestination,
  ): ProductionDestination {
    const scope = key(workflow);
    if (scope === null) unscoped = value;
    else byWorkflow.set(scope, value);
    return value;
  }

  function clear(
    workflow: object | undefined,
    expected: ProductionDestination,
  ): void {
    const scope = key(workflow);
    if (scope === null) {
      if (unscoped === expected) unscoped = undefined;
      return;
    }
    if (byWorkflow.get(scope) === expected) byWorkflow.delete(scope);
  }

  function persist(value: ProductionDestination): void {
    if (value.kind !== "project") return;
    stored = Object.freeze({
      workspaceHandle: value.workspaceHandle,
      workspaceId: value.workspaceId,
      ordinal: value.ordinal,
      nextOrdinal,
    });
    try {
      storage?.setItem(
        PRODUCTION_DESTINATION_STORAGE_KEY,
        JSON.stringify({
          schema: PRODUCTION_DESTINATION_STORAGE_KEY,
          workspace_handle: value.workspaceHandle,
          workspace_id: value.workspaceId,
          ordinal: value.ordinal,
          next_ordinal: nextOrdinal,
        }),
      );
    } catch {
      // Storage is a naming convenience only; the live binding stays authoritative.
    }
  }

  function allocate(): number {
    const ordinal = nextOrdinal;
    nextOrdinal = Math.min(nextOrdinal + 1, MAX_ORDINAL);
    return ordinal;
  }

  function adoptable(handle: string, workflow: object | undefined): boolean {
    if (unavailable.has(handle)) return false;
    const owner = owners.get(handle);
    return owner === undefined || owner === key(workflow);
  }

  return {
    /** Stable render-only preview; unlike resolve it neither adopts nor consumes an ordinal. */
    describe(
      workflow: object | undefined,
      current: ProductionWorkbenchProjection | undefined,
    ): ProductionDestination {
      const bound = read(workflow);
      if (bound !== undefined) return bound;
      if (current !== undefined && adoptable(current.workspaceHandle, workflow))
        return Object.freeze({
          kind: "project" as const,
          workspaceHandle: current.workspaceHandle,
          workspaceId: current.workspaceId,
          ordinal:
            stored?.workspaceHandle === current.workspaceHandle
              ? stored.ordinal
              : nextOrdinal,
        });
      return Object.freeze({ kind: "new" as const, ordinal: nextOrdinal });
    },

    /** The workflow's destination, adopting its visible project when nobody else owns it. */
    resolve(
      workflow: object | undefined,
      current: ProductionWorkbenchProjection | undefined,
    ): ProductionDestination {
      const bound = read(workflow);
      if (bound !== undefined) return bound;
      if (current !== undefined && adoptable(current.workspaceHandle, workflow))
        return this.bindProject(workflow, current);
      return write(
        workflow,
        Object.freeze({ kind: "new", ordinal: allocate() }),
      );
    },

    /** Read without adopting or allocating; for display. */
    peek(workflow: object | undefined): ProductionDestination | undefined {
      return read(workflow);
    },

    /** The project a Start created or appended to becomes the workflow's destination. */
    bindProject(
      workflow: object | undefined,
      project: Readonly<{ workspaceHandle: string; workspaceId: string }>,
    ): ProductionDestination {
      if (!adoptable(project.workspaceHandle, workflow))
        throw new Error("production destination belongs to another workflow");
      // CRITICAL: this mirrors the backend Production-entry capacity. Letting stale owners grow
      // beyond it can permanently refuse an otherwise available project after a release.
      if (
        !owners.has(project.workspaceHandle) &&
        owners.size >= MAX_PROJECT_OWNERS
      )
        throw new Error("production destination owner capacity exceeded");
      const bound = read(workflow);
      if (bound?.kind === "unavailable")
        throw new Error(
          "an unavailable production destination needs New project",
        );
      const ordinal =
        bound?.kind === "project" &&
        bound.workspaceHandle === project.workspaceHandle
          ? bound.ordinal
          : bound?.kind === "new"
            ? bound.ordinal
            : stored?.workspaceHandle === project.workspaceHandle
              ? stored.ordinal
              : allocate();
      owners.set(project.workspaceHandle, key(workflow));
      const value = write(
        workflow,
        Object.freeze({
          kind: "project",
          workspaceHandle: project.workspaceHandle,
          workspaceId: project.workspaceId,
          ordinal,
        }),
      );
      persist(value);
      return value;
    },

    /** Move one captured destination across the host's accepted workflow-object transition. */
    commitCapturedProject(
      sourceWorkflow: object | undefined,
      committedWorkflow: object | undefined,
      captured: ProductionDestination,
      project: Readonly<{ workspaceHandle: string; workspaceId: string }>,
    ): boolean {
      if (
        captured.kind !== "project" ||
        captured.workspaceHandle !== project.workspaceHandle ||
        captured.workspaceId !== project.workspaceId
      )
        throw new Error(
          "committed project does not match captured destination",
        );
      if (read(sourceWorkflow) !== captured) return false;

      const source = key(sourceWorkflow);
      const committed = key(committedWorkflow);
      if (source === committed) return true;
      const committedDestination = read(committedWorkflow);
      if (
        committedDestination !== undefined &&
        committedDestination !== captured
      )
        return false;
      if (owners.get(captured.workspaceHandle) !== source)
        throw new Error("captured production destination owner changed");

      // IMPORTANT: host replace commits a new workflow object after admission. Transfer only the
      // exact captured binding; a later New project must keep its newer destination and owner.
      clear(sourceWorkflow, captured);
      owners.set(captured.workspaceHandle, committed);
      write(committedWorkflow, captured);
      persist(captured);
      return true;
    },

    /** New project: the next Start creates; the prior project is neither released nor changed. */
    startNew(workflow: object | undefined): ProductionDestination {
      return write(
        workflow,
        Object.freeze({ kind: "new", ordinal: allocate() }),
      );
    },

    /** The server no longer holds the project. Nothing is created in its place automatically. */
    markUnavailable(workspaceHandle: string): void {
      unavailable.add(workspaceHandle);
      owners.delete(workspaceHandle);
      if (stored?.workspaceHandle === workspaceHandle) {
        stored = undefined;
        try {
          storage?.removeItem(PRODUCTION_DESTINATION_STORAGE_KEY);
        } catch {
          // See persist.
        }
      }
    },

    /** A released project no longer owns a workflow destination slot. */
    releaseProject(workspaceHandle: string): void {
      owners.delete(workspaceHandle);
    },

    isUnavailable(workspaceHandle: string): boolean {
      return unavailable.has(workspaceHandle);
    },

    ownedByAnotherWorkflow(
      workspaceHandle: string,
      workflow: object | undefined,
    ): boolean {
      return (
        !adoptable(workspaceHandle, workflow) &&
        !unavailable.has(workspaceHandle)
      );
    },
  };
}

export type ProductionDestinationStore = ReturnType<
  typeof createProductionDestinationStore
>;

/** Browser composition root: keep the storage seam outside entry.tsx's host registration path. */
export function createBrowserProductionDestinationStore(): ProductionDestinationStore {
  let storage: Storage | undefined;
  try {
    storage = globalThis.window?.sessionStorage;
  } catch {
    storage = undefined;
  }
  return createProductionDestinationStore({ storage });
}
