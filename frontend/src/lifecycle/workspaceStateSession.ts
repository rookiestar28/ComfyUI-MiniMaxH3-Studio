import type {
  RecoveredWorkspaceState,
  WorkspaceStateAction,
  WorkspaceStateProjection,
  WorkspaceStateRecord,
} from "../contracts/workspaceStateCodec";
import type {
  WorkspaceStateClientCode,
  createWorkspaceStateClient,
} from "../host/workspaceStateClient";
import type { ShellRuntime } from "./shellSession";

export type WorkspaceStateUiState = Readonly<{
  projection: WorkspaceStateProjection | null;
  selected: string | null;
  recovered: RecoveredWorkspaceState | null;
  busy: WorkspaceStateAction["intent"] | null;
  error: WorkspaceStateClientCode | "outcome_unknown" | null;
  confirmed: boolean;
}>;
export type WorkspaceStateBinding = Readonly<{
  state: WorkspaceStateUiState;
  ensure(): void;
  leave(): void;
  refresh(): Promise<void>;
  setEnabled(enabled: boolean): Promise<void>;
  save(): Promise<void>;
  select(recordId: string | null): void;
  restore(): Promise<void>;
  reset(): Promise<void>;
}>;
const INITIAL: WorkspaceStateUiState = Object.freeze({
  projection: null,
  selected: null,
  recovered: null,
  busy: null,
  error: null,
  confirmed: false,
});

function sameRecoveryFacts(
  record: WorkspaceStateRecord | undefined,
  recovered: RecoveredWorkspaceState | null,
): boolean {
  if (!record || !recovered) return false;
  const state = ["submitted", "running", "artifact_recorded"].includes(
    record.state,
  )
    ? "terminal_unknown_ownership"
    : record.state;
  return (
    record.record_id === recovered.record_id &&
    record.kind === recovered.kind &&
    state === recovered.state &&
    record.segment_count === recovered.segment_count &&
    (
      ["workspace", "reference", "timeline", "context", "transition"] as const
    ).every((key) => record.revisions[key] === recovered.revisions[key])
  );
}

export function createWorkspaceStateSession({
  client,
  changed,
}: {
  client: ReturnType<typeof createWorkspaceStateClient>;
  changed(): void;
}) {
  let state = INITIAL,
    generation = 0,
    active = false;
  let controller: AbortController | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const publish = (next: Partial<WorkspaceStateUiState>) => {
    state = Object.freeze({ ...state, ...next });
    changed();
  };
  const cancelPoll = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };
  const schedule = () => {
    cancelPoll();
    if (
      active &&
      state.projection?.enabled &&
      state.confirmed &&
      state.busy === null
    ) {
      timer = setTimeout(() => {
        timer = undefined;
        void refresh();
      }, 2000);
    }
  };
  async function request(action: WorkspaceStateAction): Promise<void> {
    if (!active || state.busy !== null) return;
    cancelPoll();
    const owned = generation,
      abort = new AbortController();
    controller = abort;
    publish({ busy: action.intent, error: null });
    const result = await client.send(action, abort.signal);
    // CRITICAL: release invalidates reads and commits alike. A late acknowledgement must not
    // repopulate another view; an unknown commit is reconciled by a fresh read, never replayed.
    if (!active || generation !== owned || controller !== abort) return;
    controller = undefined;
    if (!result.ok) {
      publish({
        busy: null,
        confirmed: false,
        recovered: null,
        projection: action.intent === "status" ? null : state.projection,
        error: result.outcomeUnknown ? "outcome_unknown" : result.code,
      });
      return;
    }
    const projection = result.response.projection;
    const selected =
      projection.enabled &&
      projection.records.some((row) => row.record_id === state.selected)
        ? state.selected
        : null;
    // CRITICAL: a stable record ID alone does not make an old recovery result current.
    // Keep it across polling only while all readonly facts still match the selected record.
    const retained =
      selected !== null &&
      sameRecoveryFacts(
        projection.records.find((row) => row.record_id === selected),
        state.recovered,
      )
        ? state.recovered
        : null;
    publish({
      projection,
      selected,
      busy: null,
      confirmed: true,
      error: result.response.error,
      recovered: result.response.recovered ?? retained,
    });
    schedule();
  }
  function ensure(): void {
    active = true;
    if (
      state.projection === null &&
      state.error === null &&
      state.busy === null
    )
      void refresh();
  }
  async function refresh(): Promise<void> {
    await request({ intent: "status" });
  }
  const admitted = () =>
    active &&
    state.confirmed &&
    state.busy === null &&
    state.projection?.supported === true;
  async function setEnabled(enabled: boolean): Promise<void> {
    if (!admitted()) return;
    await request({
      intent: "set_enabled",
      enabled,
      expected_revision: state.projection!.revision,
    });
  }
  async function save(): Promise<void> {
    if (!admitted() || !state.projection!.enabled) return;
    await request({
      intent: "save",
      expected_revision: state.projection!.revision,
    });
  }
  function select(recordId: string | null): void {
    if (!active || state.busy !== null) return;
    const selected =
      state.projection?.enabled &&
      state.projection.records.some((row) => row.record_id === recordId)
        ? recordId
        : null;
    publish({ selected, recovered: null });
  }
  async function restore(): Promise<void> {
    if (!admitted() || !state.projection!.enabled || state.selected === null)
      return;
    await request({
      intent: "restore",
      record_id: state.selected,
      expected_revision: state.projection!.revision,
    });
  }
  async function reset(): Promise<void> {
    if (!admitted()) return;
    await request({
      intent: "reset",
      expected_revision: state.projection!.revision,
    });
  }
  function release(): void {
    active = false;
    generation += 1;
    controller?.abort();
    controller = undefined;
    cancelPoll();
    state = INITIAL;
  }
  return Object.freeze({
    binding: (): WorkspaceStateBinding =>
      Object.freeze({
        state,
        ensure,
        leave: release,
        refresh,
        setEnabled,
        save,
        select,
        restore,
        reset,
      }),
    release,
  });
}

export function createWorkspaceStateLifecycle(ctx: ShellRuntime) {
  const client = ctx.deps.workspaceStateClient;
  const session =
    client === undefined
      ? undefined
      : createWorkspaceStateSession({
          client,
          changed: () => ctx.actions.renderCurrent(),
        });
  return Object.freeze({
    workspaceStateBinding: () => session?.binding(),
    workspaceStateLeave: () => session?.release(),
  });
}
