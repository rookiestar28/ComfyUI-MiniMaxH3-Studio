import {
  MAX_PROJECT_FILE_BYTES,
  projectFileBlob,
  type ProjectOwner,
  type ProjectPlanning,
  type ProjectOpened,
  type ProjectRelinked,
} from "../contracts/projectDocumentCodec";
import type { ProjectSnapshotOwner } from "../contracts/projectDocumentCodec";
import type { createProjectDocumentClient } from "../host/projectDocumentClient";
import type { ProjectRecoveryBinding } from "./projectRecoverySession";
export type ProjectSavePicker = () => Promise<{
  createWritable(): Promise<{
    write(blob: Blob): Promise<void>;
    close(): Promise<void>;
    abort?(): Promise<void>;
  }>;
}>;
export type ProjectFileState = Readonly<{
  status:
    | "idle"
    | "saving"
    | "saved"
    | "download"
    | "cancelled"
    | "opening"
    | "opened"
    | "relinking"
    | "error";
  title: string;
  dirty: boolean;
  error: string | null;
  missingMedia: readonly string[];
}>;
export type ProjectFileBinding = Readonly<{
  state: ProjectFileState;
  setTitle(value: string): void;
  blocked?: boolean;
  save(picker?: ProjectSavePicker): Promise<void>;
  open(file: Pick<File, "size" | "text">): Promise<void>;
  relink(asset: string, retained: string): Promise<void>;
  cancel(): void;
  previous?(): void;
  retained?: readonly Readonly<{
    asset_id: string;
    frame_count: number;
    width: number;
    height: number;
  }>[];
  refreshRetained?(): Promise<void>;
  recovery?: ProjectRecoveryBinding;
}>;
export function downloadProjectBlob(blob: Blob): void {
  const url = URL.createObjectURL(blob),
    link = document.createElement("a");
  link.href = url;
  link.download = "context-project.h3proj";
  link.hidden = true;
  document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
export function createProjectFileSession(ports: {
  client: Pick<
    ReturnType<typeof createProjectDocumentClient>,
    "export" | "open" | "relink"
  >;
  changed(): void;
  capture(): Readonly<{
    owner: ProjectSnapshotOwner | null;
    planning: ProjectPlanning;
    title: string;
    key: string;
  }>;
  adopt(value: ProjectOpened | ProjectRelinked): void;
  discard(value: ProjectOpened): void | Promise<void>;
  beforeReplace?(): Promise<boolean>;
  download?(blob: Blob): void;
}) {
  let status: ProjectFileState["status"] = "idle",
    title: string | undefined,
    error: string | null = null;
  let missingMedia: readonly string[] = [],
    acknowledged: string | null = null,
    active: AbortController | null = null;
  const publish = () => ports.changed();
  const capture = () => {
    const value = ports.capture();
    return {
      ...value,
      title: title ?? value.title,
      key: JSON.stringify([value.key, title ?? value.title]),
    };
  };
  const fail = (reason: unknown) => {
    status =
      reason instanceof DOMException && reason.name === "AbortError"
        ? "cancelled"
        : "error";
    error =
      reason instanceof Error && /^[a-z_]{1,64}$/.test(reason.message)
        ? reason.message
        : "file_operation_failed";
  };
  function cancel() {
    // CRITICAL: aborting a relink reply cannot undo a server CAS that already committed.
    const uncertain = active !== null && status === "relinking";
    active?.abort();
    active = null;
    status = uncertain ? "error" : "cancelled";
    if (uncertain) {
      error = "relink_outcome_unknown";
      acknowledged = null;
    }
    publish();
  }
  async function save(picker?: ProjectSavePicker): Promise<void> {
    if (active !== null) return;
    if (error === "relink_outcome_unknown") return;
    // CRITICAL: native picker activation must happen synchronously in the trusted click stack.
    // Catch its rejection immediately; export may still be pending when the picker is cancelled.
    let picked:
      | Promise<Awaited<ReturnType<ProjectSavePicker>> | { failure: unknown }>
      | undefined;
    try {
      picked = picker?.().catch((failure) => ({ failure }));
    } catch (reason) {
      fail(reason);
      publish();
      return;
    }
    let snapshot: ReturnType<typeof capture>;
    try {
      snapshot = capture();
    } catch (reason) {
      fail(reason);
      publish();
      return;
    }
    const abort = new AbortController();
    active = abort;
    status = "saving";
    error = null;
    publish();
    let writer:
      | Awaited<
          ReturnType<Awaited<ReturnType<ProjectSavePicker>>["createWritable"]>
        >
      | undefined;
    try {
      const result = await ports.client.export(
        snapshot.owner,
        snapshot.planning,
        snapshot.title,
        abort.signal,
      );
      const file = await picked;
      if (file && "failure" in file) throw file.failure;
      if (active !== abort || abort.signal.aborted) return;
      const blob = projectFileBlob(result.document);
      if (file !== undefined) {
        writer = await file.createWritable();
        if (active !== abort || abort.signal.aborted) {
          await writer.abort?.();
          return;
        }
        await writer.write(blob);
        await writer.close();
        writer = undefined;
        if (active !== abort || abort.signal.aborted) return;
        acknowledged = snapshot.key;
        status = "saved";
      } else {
        (ports.download ?? downloadProjectBlob)(blob);
        status = "download";
      }
    } catch (reason) {
      try {
        await writer?.abort?.();
      } catch {
        /* A failed writer must never acknowledge Saved. */
      }
      if (active === abort) fail(reason);
    } finally {
      if (active === abort) {
        active = null;
        publish();
      }
    }
  }
  async function open(file: Pick<File, "size" | "text">): Promise<void> {
    if (active !== null) return;
    if (
      !Number.isSafeInteger(file.size) ||
      file.size > MAX_PROJECT_FILE_BYTES
    ) {
      fail(new Error("document_size"));
      publish();
      return;
    }
    const abort = new AbortController();
    active = abort;
    status = "opening";
    error = null;
    publish();
    try {
      const realFile = file as Pick<File, "size" | "text"> &
        Partial<Pick<File, "arrayBuffer">>;
      const text =
        realFile.arrayBuffer === undefined
          ? await file.text()
          : new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
              await realFile.arrayBuffer(),
            );
      if (active !== abort) return;
      if (new TextEncoder().encode(text).byteLength > MAX_PROJECT_FILE_BYTES)
        throw new Error("document_size");
      if (ports.beforeReplace !== undefined && !(await ports.beforeReplace()))
        throw new Error("dirty_protected");
      if (active !== abort || abort.signal.aborted) return;
      // IMPORTANT: keep original text. JSON.parse here would erase duplicate keys before server validation.
      const response = await ports.client.open(text, abort.signal);
      if (active !== abort || abort.signal.aborted) {
        await ports.discard(response);
        return;
      }
      ports.adopt(response);
      title = response.title;
      missingMedia = response.missingMedia;
      acknowledged = capture().key;
      status = "opened";
    } catch (reason) {
      if (active === abort) fail(reason);
    } finally {
      if (active === abort) {
        active = null;
        publish();
      }
    }
  }
  async function relink(asset: string, retained: string): Promise<void> {
    if (active !== null) return;
    if (error === "relink_outcome_unknown") return;
    let snapshot: ReturnType<typeof capture>;
    try {
      snapshot = capture();
    } catch (reason) {
      fail(reason);
      publish();
      return;
    }
    if (
      snapshot.owner === null ||
      snapshot.owner.authoring_handle === null ||
      snapshot.owner.production_handle === null
    )
      return;
    const abort = new AbortController();
    active = abort;
    status = "relinking";
    error = null;
    publish();
    try {
      const response = await ports.client.relink(
        snapshot.owner,
        asset,
        retained,
        abort.signal,
      );
      if (active !== abort || abort.signal.aborted) return;
      ports.adopt(response);
      missingMedia = response.missingMedia;
      status = "opened";
    } catch (reason) {
      if (active === abort) {
        fail(reason);
        if (
          error === "malformed_response" ||
          error === "cross_workspace_response" ||
          !(reason instanceof Error && /^[a-z_]{1,64}$/.test(reason.message))
        ) {
          status = "error";
          error = "relink_outcome_unknown";
          acknowledged = null;
        }
      }
    } finally {
      if (active === abort) {
        active = null;
        publish();
      }
    }
  }
  function binding(): ProjectFileBinding {
    let current: ReturnType<typeof capture> | undefined;
    try {
      current = capture();
    } catch {
      /* An unpaired editor may still open a different file. */
    }
    return {
      state: {
        status,
        title: current?.title ?? title ?? "",
        dirty: current === undefined || acknowledged !== current.key,
        error,
        missingMedia,
      },
      setTitle(value) {
        title = value.slice(0, 256);
        publish();
      },
      save,
      open,
      relink,
      cancel,
    };
  }
  function restoreMetadata(
    value: Pick<ProjectFileState, "title" | "missingMedia">,
  ) {
    title = value.title;
    missingMedia = value.missingMedia;
    acknowledged = null;
    status = "idle";
    error = null;
  }
  return {
    binding,
    save,
    open,
    relink,
    cancel,
    restoreMetadata,
    dispose: cancel,
  };
}
