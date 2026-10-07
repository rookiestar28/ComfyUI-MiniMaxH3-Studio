import type {
  ProjectOpened,
  ProjectPlanning,
  ProjectSnapshotOwner,
} from "../contracts/projectDocumentCodec";
import type {
  RecoveryRecord,
  RecoveryStatus,
} from "../contracts/projectRecoveryCodec";
import type { createProjectRecoveryClient } from "../host/projectRecoveryClient";

type Capture = Readonly<{
  owner: ProjectSnapshotOwner | null;
  planning: ProjectPlanning;
  title: string;
}>;
export type ProjectRecoveryBinding = Readonly<{
  state: Readonly<{
    enabled: boolean;
    includeVideo: boolean;
    busy: boolean;
    dirty: boolean;
    status: "off" | "idle" | "dirty" | "saving" | "saved" | "error";
    error: string | null;
    records: readonly RecoveryRecord[];
    savedAt: number | null;
  }>;
  settings(enabled: boolean, includeVideo: boolean): Promise<void>;
  saveNow(): Promise<void>;
  refresh(): Promise<void>;
  restore(project: string): Promise<void>;
  clear(project: string): Promise<void>;
  beforeReplace(discard: boolean): Promise<boolean>;
}>;
function sameOwner(
  a: ProjectSnapshotOwner | null,
  b: ProjectSnapshotOwner | null,
): boolean {
  if (a === null || b === null) return a === b;
  return Object.keys(a).every(
    (key) =>
      a[key as keyof ProjectSnapshotOwner] ===
      b[key as keyof ProjectSnapshotOwner],
  );
}
export function createProjectRecoverySession(ports: {
  client: ReturnType<typeof createProjectRecoveryClient>;
  capture(): Capture;
  changed(): void;
  adopt(value: ProjectOpened): void;
  discard(value: ProjectOpened): void | Promise<void>;
  visible(): boolean;
}) {
  let view: RecoveryStatus | null = null,
    projectId: string | null = null;
  let admittedKey: string | null = null,
    admittedGeneration = 0;
  let suspendedKey: string | null = null;
  let error: string | null = null,
    active = false,
    left = false,
    disposed = false;
  let flushAfterLeave = false;
  let poll: ReturnType<typeof setTimeout> | undefined,
    draft: ReturnType<typeof setTimeout> | undefined;
  const key = (value: Capture) =>
    JSON.stringify([value.owner, value.planning, value.title]);
  const publish = () => {
    if (!disposed) ports.changed();
  };
  const reason = (failure: unknown) =>
    failure instanceof Error && /^[a-z_]{1,64}$/.test(failure.message)
      ? failure.message
      : "recovery_unavailable";
  function dirty(): boolean {
    if (!view?.enabled) return false;
    try {
      if (projectId === null && key(ports.capture()) === suspendedKey)
        return false;
      return (
        key(ports.capture()) !== admittedKey ||
        view.current === null ||
        view.current.generation !== view.current.saved_generation ||
        view.current.saved_generation < admittedGeneration ||
        !sameOwner(view.current.saved_owner, ports.capture().owner)
      );
    } catch {
      return true;
    }
  }
  function accept(value: RecoveryStatus) {
    view = value;
    if (value.current !== null) {
      if (projectId !== null && projectId !== value.current.project_id)
        throw new Error("cross_workspace_response");
      projectId = value.current.project_id;
    }
  }
  function schedulePoll() {
    if (
      !disposed &&
      !left &&
      ports.visible() &&
      view?.enabled &&
      projectId !== null &&
      poll === undefined
    )
      poll = setTimeout(() => {
        poll = undefined;
        void refresh();
      }, 1000);
  }
  async function operation(work: () => Promise<void>): Promise<void> {
    if (active || disposed) return;
    active = true;
    error = null;
    publish();
    try {
      await work();
    } catch (failure) {
      error = reason(failure);
    } finally {
      active = false;
      publish();
      schedulePoll();
      if (left && flushAfterLeave && !disposed) {
        flushAfterLeave = false;
        if (error === null && dirty()) void saveNow();
      }
    }
  }
  async function watch() {
    if (!view?.enabled) return;
    const captured = ports.capture(),
      signature = key(captured);
    if (signature === admittedKey && projectId !== null) return;
    const reply = await ports.client.send({
      intent: "watch",
      ...captured,
      project_id: projectId,
    });
    if (reply.current === null) throw new Error("project_unavailable");
    accept(reply);
    admittedKey = signature;
    admittedGeneration = reply.current.generation;
    // CRITICAL: an admitted old draft cannot acknowledge a newer browser edit as Saved.
    // Status must match both the admitted generation and the current revision/draft key.
    publish();
  }
  async function refresh() {
    await operation(async () => {
      accept(
        await ports.client.send({ intent: "status", project_id: projectId }),
      );
    });
  }
  function observe() {
    if (disposed || !ports.visible()) return;
    left = false;
    flushAfterLeave = false;
    if (view === null) {
      if (error === null) void refresh();
      return;
    }
    if (!view.enabled || active || error !== null) return;
    let changed = false;
    try {
      changed = key(ports.capture()) !== admittedKey;
    } catch {
      return;
    }
    if (changed && key(ports.capture()) !== suspendedKey && draft === undefined)
      draft = setTimeout(() => {
        draft = undefined;
        void operation(watch);
      }, 250);
    schedulePoll();
  }
  async function saveNow() {
    clearTimeout(draft);
    draft = undefined;
    await operation(async () => {
      await watch();
      if (projectId !== null)
        accept(
          await ports.client.send({ intent: "flush", project_id: projectId }),
        );
    });
  }
  async function settings(enabled: boolean, includeVideo: boolean) {
    await operation(async () => {
      if (view === null)
        accept(
          await ports.client.send({ intent: "status", project_id: projectId }),
        );
      if (!enabled && dirty()) throw new Error("dirty_protected");
      accept(
        await ports.client.send({
          intent: "settings",
          enabled,
          include_video: includeVideo,
          expected_revision: view!.revision,
        }),
      );
      if (!enabled) {
        projectId = null;
        admittedKey = null;
        admittedGeneration = 0;
      } else {
        suspendedKey = null;
        await watch();
      }
    });
  }
  async function beforeReplace(discard: boolean): Promise<boolean> {
    if (active) return false;
    if (!view?.enabled || (projectId === null && !dirty())) return true;
    if (!discard) {
      await saveNow();
      if (active || dirty() || error !== null) return false;
    }
    await operation(async () => {
      if (projectId !== null)
        accept(
          await ports.client.send({
            intent: "detach",
            project_id: projectId,
            discard,
          }),
        );
      suspendedKey = key(ports.capture());
      projectId = null;
      admittedKey = null;
      admittedGeneration = 0;
      if (view !== null) view = { ...view, current: null };
    });
    return error === null;
  }
  async function restore(project: string) {
    if (!(await beforeReplace(false))) return;
    await operation(async () => {
      const value = await ports.client.restore(project);
      if (disposed) {
        await ports.discard(value);
        return;
      }
      ports.adopt(value);
      projectId = null;
      admittedKey = null;
      suspendedKey = null;
      admittedGeneration = 0;
      accept(await ports.client.send({ intent: "status", project_id: null }));
    });
    observe();
  }
  async function clear(project: string) {
    await operation(async () => {
      accept(await ports.client.send({ intent: "clear", project_id: project }));
      if (projectId !== null)
        accept(
          await ports.client.send({ intent: "status", project_id: projectId }),
        );
    });
  }
  function leave() {
    left = true;
    clearTimeout(poll);
    clearTimeout(draft);
    poll = draft = undefined;
    // Best effort only. The backend writer/dirty lease is authoritative without this browser.
    if (view?.enabled && dirty()) {
      // IMPORTANT: one pending leave flush admits a later draft after the old request ends.
      // Do not retry failed saves indefinitely or claim browser unload is durable authority.
      if (active) flushAfterLeave = true;
      else void saveNow();
    }
  }
  function binding(): ProjectRecoveryBinding {
    const isDirty = dirty();
    return {
      state: {
        enabled: view?.enabled ?? false,
        includeVideo: view?.include_video ?? false,
        busy: active,
        dirty: isDirty,
        status:
          error !== null || view?.current?.state === "error"
            ? "error"
            : !view?.enabled
              ? "off"
              : active || view.current?.state === "saving"
                ? "saving"
                : isDirty
                  ? "dirty"
                  : view.current === null
                    ? "idle"
                    : "saved",
        error: error ?? view?.current?.reason ?? null,
        records: view?.records ?? [],
        savedAt: view?.current?.saved_at_ms ?? null,
      },
      settings,
      saveNow,
      refresh,
      restore,
      clear,
      beforeReplace,
    };
  }
  return {
    binding,
    observe,
    refresh,
    settings,
    saveNow,
    beforeReplace,
    leave,
    dispose() {
      leave();
      disposed = true;
    },
  };
}
