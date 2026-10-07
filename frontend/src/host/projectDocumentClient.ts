import {
  decodeProjectReply,
  MAX_PROJECT_RESPONSE_BYTES,
  MAX_PROJECT_REQUEST_BYTES,
  type ProjectOwner,
  type ProjectPlanning,
  type ProjectExport,
  type ProjectOpened,
  type ProjectRelinked,
  type ProjectSnapshotOwner,
} from "../contracts/projectDocumentCodec";
import { readExactByob } from "./authoringMediaPreview";
export function createProjectDocumentClient({
  fetchApi,
}: {
  fetchApi(path: string, init: RequestInit): Promise<Response>;
}) {
  async function send(
    intent: "export" | "open" | "relink",
    payload: Record<string, unknown>,
    signal?: AbortSignal,
  ) {
    const body = JSON.stringify({ intent, ...payload });
    if (new TextEncoder().encode(body).byteLength > MAX_PROJECT_REQUEST_BYTES)
      throw new Error("document_size");
    const response = await fetchApi("/h3-context/project-document", {
      method: "POST",
      body,
      signal,
      headers: { "content-type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    const raw = response.headers.get("content-length");
    if (
      response.headers.get("cache-control") !== "no-store" ||
      !raw ||
      !/^[1-9][0-9]*$/.test(raw) ||
      Number(raw) > MAX_PROJECT_RESPONSE_BYTES ||
      !response.headers.get("content-type")?.startsWith("application/json") ||
      response.redirected ||
      response.headers.has("content-encoding")
    ) {
      await response.body?.cancel();
      throw new Error("malformed_response");
    }
    const bytes = await readExactByob(response, Number(raw));
    const value = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(bytes),
    );
    if (response.status !== 200) {
      const code =
        value !== null &&
        typeof value === "object" &&
        typeof value.code === "string" &&
        /^[a-z_]{1,64}$/.test(value.code)
          ? value.code
          : "project_unavailable";
      throw new Error(code);
    }
    return decodeProjectReply(value, intent);
  }
  return {
    async discard(owner: ProjectOwner): Promise<void> {
      const response = await fetchApi("/h3-context/project-document", {
        method: "POST",
        body: JSON.stringify({ intent: "discard", owner }),
        headers: { "content-type": "application/json" },
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
      });
      await response.body?.cancel();
    },
    async export(
      owner: ProjectSnapshotOwner | null,
      planning: ProjectPlanning,
      title: string,
      signal?: AbortSignal,
    ): Promise<ProjectExport> {
      const result = (await send(
        "export",
        { owner, planning, title },
        signal,
      )) as ProjectExport;
      if (JSON.stringify(result.owner) !== JSON.stringify(owner)) {
        const keys =
          owner === null ? [] : (Object.keys(owner) as (keyof ProjectOwner)[]);
        if (
          owner === null ||
          result.owner === null ||
          keys.some((key) => owner[key] !== result.owner![key])
        )
          throw new Error("cross_workspace_response");
      }
      return result;
    },
    async open(document: string, signal?: AbortSignal): Promise<ProjectOpened> {
      return (await send("open", { document }, signal)) as ProjectOpened;
    },
    async relink(
      owner: ProjectOwner,
      asset_id: string,
      retained_id: string,
      signal?: AbortSignal,
    ): Promise<ProjectRelinked> {
      const result = (await send(
        "relink",
        { owner, asset_id, retained_id },
        signal,
      )) as ProjectRelinked;
      if (
        result.owner.production_handle !== owner.production_handle ||
        result.owner.authoring_handle !== owner.authoring_handle ||
        result.owner.production_id !== owner.production_id ||
        result.owner.production_revision !== owner.production_revision + 1 ||
        result.owner.production_fingerprint !== owner.production_fingerprint ||
        result.owner.timeline_revision !== owner.timeline_revision ||
        result.owner.workspace_revision !== owner.workspace_revision ||
        result.owner.authoring_fingerprint !== owner.authoring_fingerprint
      )
        throw new Error("cross_workspace_response");
      return result;
    },
  };
}
