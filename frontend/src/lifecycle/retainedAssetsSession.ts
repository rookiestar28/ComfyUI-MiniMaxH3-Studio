import type {
  RetainedAssetsAction,
  RetainedAssetsProjection,
  RestoredAsset,
  RetainSelection,
} from "../contracts/retainedAssetsCodec";
import { retainSelectionKey } from "../contracts/retainedAssetsCodec";
import type {
  createRetainedAssetsClient,
  RetainedAssetsClientCode,
} from "../host/retainedAssetsClient";
import type { ShellRuntime } from "./shellSession";

export type RetainedAssetsUiState = Readonly<{
  projection: RetainedAssetsProjection | null;
  selected: string | null;
  restored: RestoredAsset | null;
  previewUrl: string | null;
  busy: RetainedAssetsAction["intent"] | "preview" | null;
  error: RetainedAssetsClientCode | "outcome_unknown" | null;
  confirmed: boolean;
  retainedId: string | null;
  retainedKey: string | null;
  cleanup: Readonly<{ removed: number; protected: number }> | null;
}>;
export type RetainedAssetsBinding = Readonly<{
  state: RetainedAssetsUiState;
  ensure(): void;
  leave(): void;
  refresh(): Promise<void>;
  setEnabled(enabled: boolean): Promise<void>;
  select(id: string | null): void;
  retain(selection: RetainSelection): Promise<void>;
  restore(): Promise<void>;
  preview(): Promise<void>;
  releaseUse(): Promise<void>;
  clear(): Promise<void>;
  collect(): Promise<void>;
}>;
const INITIAL: RetainedAssetsUiState = Object.freeze({
  projection: null,
  selected: null,
  restored: null,
  previewUrl: null,
  busy: null,
  error: null,
  confirmed: false,
  retainedId: null,
  retainedKey: null,
  cleanup: null,
});
export function createRetainedAssetsSession({
  client,
  changed,
}: {
  client: ReturnType<typeof createRetainedAssetsClient>;
  changed(): void;
}) {
  let state = INITIAL,
    active = false,
    generation = 0;
  let controller: AbortController | undefined;
  const publish = (next: Partial<RetainedAssetsUiState>) => {
    state = Object.freeze({ ...state, ...next });
    changed();
  };
  const dropUrl = () => {
    if (state.previewUrl !== null) URL.revokeObjectURL(state.previewUrl);
  };
  const releaseHandle = async (handle: string) => {
    // CRITICAL: cleanup is independent of the aborted media request. Never reuse its signal.
    return client.send({ intent: "release", use_handle: handle });
  };
  const dropUse = () => {
    dropUrl();
    const use = state.restored;
    if (use !== null) void releaseHandle(use.use_handle);
    return { restored: null, previewUrl: null } as const;
  };
  async function request(action: RetainedAssetsAction): Promise<void> {
    if (!active || state.busy !== null) return;
    const owned = generation,
      abort = new AbortController();
    controller = abort;
    publish({
      busy: action.intent,
      error: null,
      cleanup: null,
      retainedId: null,
      retainedKey: null,
    });
    const result = await client.send(action, abort.signal);
    // CRITICAL: a late restore may own a newly issued server resource. Release it even when
    // the view generation is gone; never repopulate a hidden/new view or replay a mutation.
    if (!active || owned !== generation || controller !== abort) {
      if (result.ok && result.response.restored !== null)
        await releaseHandle(result.response.restored.use_handle);
      return;
    }
    controller = undefined;
    if (!result.ok) {
      publish({
        busy: null,
        confirmed: false,
        error: result.outcomeUnknown ? "outcome_unknown" : result.code,
        ...dropUse(),
      });
      return;
    }
    const projection = result.response.projection;
    const selected =
      projection.enabled &&
      projection.assets.some(
        (row) => row.asset_id === state.selected && row.state !== "expired",
      )
        ? state.selected
        : null;
    const retained =
      state.restored === null
        ? null
        : projection.assets.find(
            (row) => row.asset_id === state.restored!.asset_id,
          );
    const matches =
      projection.enabled &&
      retained !== undefined &&
      retained !== null &&
      retained.state !== "expired" &&
      retained.width === state.restored?.width &&
      retained.height === state.restored?.height &&
      retained.frame_count === state.restored?.frame_count;
    const discarded = !matches && state.restored !== null ? dropUse() : {};
    publish({
      ...discarded,
      projection,
      selected,
      busy: null,
      confirmed: true,
      error: null,
      ...(result.response.restored !== null
        ? { restored: result.response.restored, previewUrl: null }
        : {}),
      retainedId: result.response.retained_id,
      retainedKey:
        action.intent === "retain" && result.response.retained_id !== null
          ? retainSelectionKey(action)
          : null,
      cleanup: result.response.cleanup,
    });
  }
  function ensure() {
    active = true;
    if (
      state.projection === null &&
      state.error === null &&
      state.busy === null
    )
      void refresh();
  }
  async function refresh() {
    await request({ intent: "status" });
  }
  const admitted = () =>
    active &&
    state.confirmed &&
    state.busy === null &&
    state.projection?.supported === true;
  async function setEnabled(enabled: boolean) {
    if (!admitted()) return;
    publish(dropUse());
    await request({
      intent: "set_enabled",
      enabled,
      expected_revision: state.projection!.revision,
    });
  }
  function select(id: string | null) {
    if (!active || state.busy !== null) return;
    const selected =
      state.projection?.enabled &&
      state.projection.assets.some(
        (row) => row.asset_id === id && row.state !== "expired",
      )
        ? id
        : null;
    if (selected !== state.selected)
      publish({ selected, ...dropUse(), error: null });
  }
  async function retain(selection: RetainSelection) {
    if (!admitted() || !state.projection!.enabled) return;
    await request({
      intent: "retain",
      expected_revision: state.projection!.revision,
      ...selection,
    });
  }
  async function restore() {
    if (
      !admitted() ||
      !state.projection!.enabled ||
      state.selected === null ||
      state.restored !== null
    )
      return;
    await request({
      intent: "restore",
      asset_id: state.selected,
      expected_revision: state.projection!.revision,
    });
  }
  async function preview() {
    if (!admitted() || !state.restored?.preview_available) return;
    dropUrl();
    const use = state.restored,
      owned = generation,
      abort = new AbortController();
    controller = abort;
    publish({ busy: "preview", previewUrl: null, error: null });
    const result = await client.preview(use.use_handle, abort.signal);
    if (
      !active ||
      owned !== generation ||
      controller !== abort ||
      state.restored !== use
    )
      return;
    controller = undefined;
    if (!result.ok) {
      publish({ busy: null, error: result.code, ...dropUse() });
      return;
    }
    publish({ busy: null, previewUrl: URL.createObjectURL(result.blob) });
  }
  async function releaseUse() {
    const use = state.restored;
    if (!active || use === null) return;
    generation += 1;
    const owned = generation;
    controller?.abort();
    controller = undefined;
    dropUrl();
    // CRITICAL: revocation is still in flight after dropping the local use. Keep new commands
    // blocked until its acknowledgement so late cleanup cannot overwrite a newer operation.
    publish({ restored: null, previewUrl: null, busy: "release", error: null });
    const result = await releaseHandle(use.use_handle);
    if (!active || owned !== generation) return;
    if (result.ok)
      publish({
        busy: null,
        projection: result.response.projection,
        confirmed: true,
        error: null,
      });
    else
      publish({
        busy: null,
        confirmed: false,
        error: result.outcomeUnknown ? "outcome_unknown" : result.code,
      });
  }
  async function clear() {
    if (admitted())
      await request({
        intent: "clear",
        expected_revision: state.projection!.revision,
      });
  }
  async function collect() {
    if (admitted())
      await request({
        intent: "collect",
        expected_revision: state.projection!.revision,
      });
  }
  function release() {
    active = false;
    generation += 1;
    controller?.abort();
    controller = undefined;
    dropUse();
    state = INITIAL;
  }
  return Object.freeze({
    binding: (): RetainedAssetsBinding =>
      Object.freeze({
        state,
        ensure,
        leave: release,
        refresh,
        setEnabled,
        select,
        retain,
        restore,
        preview,
        releaseUse,
        clear,
        collect,
      }),
    release,
  });
}
export function createRetainedAssetsLifecycle(ctx: ShellRuntime) {
  const session =
    ctx.deps.retainedAssetsClient === undefined
      ? undefined
      : createRetainedAssetsSession({
          client: ctx.deps.retainedAssetsClient,
          changed: () => ctx.actions.renderCurrent(),
        });
  return Object.freeze({
    retainedAssetsBinding: () => session?.binding(),
    retainedAssetsLeave: () => session?.release(),
  });
}
