import {
  decodeRecoveryStatus,
  recoveryProject,
  type RecoveryStatus,
} from "../contracts/projectRecoveryCodec";
import {
  decodeProjectReply,
  MAX_PROJECT_REQUEST_BYTES,
  MAX_PROJECT_RESPONSE_BYTES,
  type ProjectOpened,
} from "../contracts/projectDocumentCodec";
import { readExactByob } from "./authoringMediaPreview";

export function createProjectRecoveryClient({
  fetchApi,
}: {
  fetchApi(path: string, init: RequestInit): Promise<Response>;
}) {
  async function request(
    action: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<unknown> {
    const body = JSON.stringify(action);
    if (new TextEncoder().encode(body).byteLength > MAX_PROJECT_REQUEST_BYTES)
      throw new Error("document_size");
    const response = await fetchApi("/h3-context/project-recovery", {
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
    const value = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(
        await readExactByob(response, Number(raw)),
      ),
    );
    if (response.status !== 200) {
      const code =
        value?.schema === "h3.context.project_recovery.error.v1" &&
        typeof value.code === "string" &&
        /^[a-z_]{1,64}$/.test(value.code)
          ? value.code
          : "recovery_unavailable";
      throw new Error(code);
    }
    return value;
  }
  return {
    async send(
      action: Record<string, unknown>,
      signal?: AbortSignal,
    ): Promise<RecoveryStatus> {
      if (action.intent === "restore") throw new Error("command_invalid");
      return decodeRecoveryStatus(await request(action, signal));
    },
    async restore(
      project_id: string,
      signal?: AbortSignal,
    ): Promise<ProjectOpened> {
      recoveryProject(project_id);
      return decodeProjectReply(
        await request({ intent: "restore", project_id }, signal),
        "open",
      ) as ProjectOpened;
    },
  };
}
